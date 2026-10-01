"""Interview question generation job (CT-39; PLAN-12, PLAN-13, PLAN-14, PLAN-92, KNOW-05,
KNOW-06, KNOW-90, KNOW-92).

``prepare_questions`` is the handler of the ``session.prepare_questions`` job queued by
``confirm_plan``. For each skill of the confirmed plan it looks up sources in the already
collected knowledge base (full-text search only, no network request) and asks the LLM for the
N questions in English, each with its main skill and the reference points written before any
answer. The resume snapshot is the context; skills missing from it still get a question.

The generated plan is validated deterministically: exactly N questions, one per plan skill,
non-empty text and reference points, and distinct texts. An invalid plan, like an unavailable
inference server, is retried up to ``llm_max_attempts`` times. A skill without sources gets
``no_verified_source=True`` and ``sources=[]``.

Outcomes, written in the worker's session, which is never committed here (CT-7):

- success: the questions are stored and the session moves to ``in_interview``;
- attempts exhausted: the session moves to ``preparation_failed``; the confirmed list, the
  snapshot and ``planned_count`` are kept, so ``retry_preparation`` can queue it again.

The LLM runs without any row lock. Afterwards the session row is locked again with
``SELECT ... FOR UPDATE`` and its status re-checked, so nothing is written to a session
cancelled or expired meanwhile. An unexpected error (e.g. database) marks the session
``preparation_failed`` in a fresh session and is raised again, so a session never stays
``preparing_questions`` once its job failed. Logs carry ids, counts and codes only: never
prompts, resume content, questions nor reference points.
"""

import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_sessionmaker
from app.errors import INVALID_STATE, AppError
from app.interviews.requirement_list import PREPARE_QUESTIONS_JOB, PlanProposal
from app.interviews.sessions import touch_activity
from app.interviews.state_machine import transition
from app.jobs.queue import enqueue
from app.knowledge.retrieval import search_for_skill
from app.llm.client import (
    LLMClient,
    LLMInvalidOutput,
    LLMUnavailable,
    get_llm_client,
    run_with_attempts,
)
from app.llm.untrusted import UNTRUSTED_RULES, wrap_untrusted
from app.models.interview import (
    ExpectedLevel,
    InterviewSession,
    Question,
    RequirementItem,
    SessionStatus,
)
from app.models.knowledge import SourceRef
from app.observability import log_event

__all__ = [
    "LLM_UNAVAILABLE",
    "PLAN_INCONSISTENT",
    "PLAN_INVALID",
    "PREPARE_QUESTIONS_JOB",
    "QUESTION_GENERATION_TASK",
    "UNEXPECTED_ERROR",
    "prepare_questions",
    "retry_preparation",
]

QUESTION_GENERATION_TASK = "question_generation"

# Failure codes, logged only (the session status is what the candidate sees).
LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
PLAN_INVALID = "PLAN_INVALID"
PLAN_INCONSISTENT = "PLAN_INCONSISTENT"
UNEXPECTED_ERROR = "UNEXPECTED_ERROR"

SOURCES_PER_SKILL = 3
FAILURE_MARK_LOCK_TIMEOUT = "5s"

# Defensive bounds on model output.
QUESTION_TEXT_MAX_LENGTH = 4000
REFERENCE_POINT_MAX_LENGTH = 1000
MAX_REFERENCE_POINTS = 20

# NUL is dropped (PostgreSQL text/JSONB cannot hold it); other C0/C1 controls (except tab and
# newlines) and lone surrogates (not encodable as UTF-8) become spaces.
_NUL_RE = re.compile("\x00")
_CONTROL_RE = re.compile("[\x01-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")

_SYSTEM_PROMPT = (
    "You are a senior technical interviewer preparing a technical interview.\n"
    "Write exactly one question for each skill of the interview plan, in English, adequate "
    "to the expected level of that skill and to the candidate resume summary. Skills that are "
    "absent from the resume still get a question.\n"
    '- Each question assesses one main skill, given in the field "skill" exactly as written '
    "in the plan.\n"
    '- For each question, list in "reference_points" the essential points, in English, that '
    "a good answer must cover. They are kept hidden from the candidate.\n"
    "- Every question text must be different.\n"
    "- Use the technical sources, when given, as the factual basis of the questions and the "
    "reference points.\n"
    'Return JSON: {"questions": [{"skill": ..., "text": ..., "reference_points": [...]}]}.\n\n'
    + UNTRUSTED_RULES
)


class _GeneratedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    text: str
    reference_points: list[str]


class _GeneratedPlan(BaseModel):
    """Answer expected from the model."""

    model_config = ConfigDict(extra="forbid")

    questions: list[_GeneratedQuestion]


@dataclass(frozen=True)
class _PlannedSkill:
    name: str
    level: ExpectedLevel | None
    sources: list[SourceRef]


@dataclass(frozen=True)
class _ValidQuestion:
    text: str
    reference_points: list[str]


class _PlanInconsistent(Exception):
    """The stored plan cannot be prepared (e.g. ``planned_count`` differs from its skills)."""


def _sanitize(text: str) -> str:
    return _CONTROL_RE.sub(" ", _NUL_RE.sub("", text)).strip()


def _key(text: str) -> str:
    return " ".join(text.split()).casefold()


def _parse_payload(payload: dict[str, str]) -> uuid.UUID:
    raw = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(raw, str):
        raise ValueError("session.prepare_questions payload requires a session_id")
    try:
        return uuid.UUID(raw)
    except ValueError:
        # The raw value is never echoed back.
        raise ValueError("session.prepare_questions payload has an invalid session_id") from None


def _duration_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


# --- plan and prompt ---------------------------------------------------------------------------


def _plan_skills(db: Session, session: InterviewSession) -> list[_PlannedSkill]:
    """Return the confirmed plan skills with their level and knowledge base sources."""
    if not session.proposal or not session.planned_count:
        raise _PlanInconsistent
    proposal = PlanProposal.model_validate(session.proposal)
    skill_keys = [_key(skill) for skill in proposal.skills]
    if (
        len(proposal.skills) != session.planned_count
        or len(set(skill_keys)) != len(skill_keys)
        or any(not key for key in skill_keys)
    ):
        raise _PlanInconsistent

    levels: dict[str, ExpectedLevel | None] = {}
    for raw in session.requirement_items or []:
        item = RequirementItem.model_validate(raw)
        if item.classification == "required":
            levels.setdefault(_key(item.name), item.level)

    return [
        _PlannedSkill(
            name=skill,
            level=levels.get(_key(skill)) or session.interview_level,
            # Read-only full-text search over the collected base: no network (KNOW-05).
            sources=search_for_skill(db, skill, SOURCES_PER_SKILL),
        )
        for skill in proposal.skills
    ]


def _resume_context(snapshot: list[dict[str, Any]] | None) -> str:
    # Only the structured fields: the snapshot may be minimal or empty (LAC-41).
    summary = [
        {"kind": entry.get("kind"), "fields": entry.get("fields")}
        for entry in snapshot or []
        if isinstance(entry, dict)
    ]
    return json.dumps(summary, ensure_ascii=False, default=str)


def _build_user_prompt(skills: list[_PlannedSkill], snapshot: list[dict[str, Any]]) -> str:
    parts = [f"Number of questions: {len(skills)}."]
    for index, skill in enumerate(skills, start=1):
        level = skill.level.value if skill.level is not None else "not specified"
        lines = [
            f"Plan skill {index} (expected level: {level}):",
            wrap_untrusted("skill", skill.name),
        ]
        if skill.sources:
            lines.append("Technical sources for this skill:")
            lines.extend(
                wrap_untrusted("source_excerpt", f"{source.title}\n{source.excerpt}")
                for source in skill.sources
            )
        else:
            lines.append("No technical source is available for this skill.")
        parts.append("\n".join(lines))
    parts.append(
        "Candidate resume summary (may be empty):\n"
        + wrap_untrusted("resume_snapshot", _resume_context(snapshot))
    )
    return "\n\n".join(parts)


# --- deterministic validation (PLAN-13) --------------------------------------------------------


def _validated_plan(output: _GeneratedPlan, skills: list[_PlannedSkill]) -> list[_ValidQuestion]:
    """Return one valid question per plan skill, in plan order, or raise ``LLMInvalidOutput``."""
    if len(output.questions) != len(skills):
        raise LLMInvalidOutput("question count differs from the plan")

    by_skill: dict[str, _ValidQuestion] = {}
    seen_texts: set[str] = set()
    plan_keys = {_key(skill.name) for skill in skills}
    for generated in output.questions:
        skill_key = _key(_sanitize(generated.skill))
        if not skill_key or skill_key not in plan_keys or skill_key in by_skill:
            raise LLMInvalidOutput("question without a distinct plan skill")
        text = _sanitize(generated.text)
        if not text or len(text) > QUESTION_TEXT_MAX_LENGTH:
            raise LLMInvalidOutput("question text is empty or too long")
        if _key(text) in seen_texts:
            raise LLMInvalidOutput("repeated question text")
        points = [_sanitize(point) for point in generated.reference_points]
        points = [point for point in points if point]
        if (
            not points
            or len(points) > MAX_REFERENCE_POINTS
            or any(len(point) > REFERENCE_POINT_MAX_LENGTH for point in points)
        ):
            raise LLMInvalidOutput("question without valid reference points")
        seen_texts.add(_key(text))
        by_skill[skill_key] = _ValidQuestion(text=text, reference_points=points)

    return [by_skill[_key(skill.name)] for skill in skills]


def _generate(
    llm: LLMClient, skills: list[_PlannedSkill], snapshot: list[dict[str, Any]]
) -> list[_ValidQuestion]:
    user_prompt = _build_user_prompt(skills, snapshot)

    def attempt() -> list[_ValidQuestion]:
        output = llm.complete_structured(
            QUESTION_GENERATION_TASK, _SYSTEM_PROMPT, user_prompt, _GeneratedPlan
        )
        return _validated_plan(output, skills)

    return run_with_attempts(
        attempt, get_settings().llm_max_attempts, (LLMInvalidOutput, LLMUnavailable)
    )


# --- persistence -------------------------------------------------------------------------------


def _lock_session(db: Session, session_id: uuid.UUID) -> InterviewSession | None:
    statement = (
        select(InterviewSession)
        .where(InterviewSession.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return db.execute(statement).scalar_one_or_none()


def _store_questions(
    db: Session,
    session: InterviewSession,
    skills: list[_PlannedSkill],
    questions: list[_ValidQuestion],
) -> None:
    for position, (skill, question) in enumerate(zip(skills, questions, strict=True), start=1):
        db.add(
            Question(
                session_id=session.id,
                position=position,
                skill_name=skill.name,
                expected_level=skill.level,
                text=question.text,
                reference_points=list(question.reference_points),
                sources=[source.model_dump(mode="json") for source in skill.sources],
                no_verified_source=not skill.sources,
            )
        )


def _fail_after_error(session_id: uuid.UUID, error: Exception) -> None:
    """Mark a preparing session ``preparation_failed`` in a fresh session (PLAN-92).

    The worker session may be broken (e.g. lost connection), so a new one is used. A short
    lock timeout keeps the worker from hanging when the row is locked elsewhere.
    """
    try:
        with get_sessionmaker()() as fresh:
            fresh.execute(sql_text(f"SET LOCAL lock_timeout = '{FAILURE_MARK_LOCK_TIMEOUT}'"))
            changed = fresh.execute(
                update(InterviewSession)
                .where(
                    InterviewSession.id == session_id,
                    InterviewSession.status == SessionStatus.PREPARING_QUESTIONS,
                )
                .values(status=SessionStatus.PREPARATION_FAILED)
            ).rowcount  # type: ignore[attr-defined]
            fresh.commit()
    except Exception as mark_error:
        log_event(
            "session.preparation_failure_mark_failed",
            session_id=str(session_id),
            error_code=type(mark_error).__name__,
        )
        return
    if changed:
        log_event(
            "session.preparation_failed",
            session_id=str(session_id),
            failure_code=UNEXPECTED_ERROR,
            error_code=type(error).__name__,
        )


# --- public API --------------------------------------------------------------------------------


def prepare_questions(db: Session, payload: dict[str, str]) -> None:
    """Job handler for ``session.prepare_questions``. Does not commit (CT-7).

    Only sessions in ``preparing_questions`` are processed; any other state (already prepared,
    cancelled, expired, unknown id) is skipped. An unexpected error marks the session
    ``preparation_failed`` in a fresh session and is raised again.
    """
    session_id = _parse_payload(payload)
    try:
        _prepare(db, session_id)
    except Exception as error:
        # Release any row lock held by the worker session before the fresh session writes.
        db.rollback()
        _fail_after_error(session_id, error)
        raise


def _llm_client() -> LLMClient:
    try:
        return get_llm_client()
    except ValueError as error:
        # Misconfigured inference server (e.g. host outside llm_allowed_hosts).
        log_event("session.llm_client_unavailable", reason="invalid_configuration")
        raise LLMUnavailable("inference server misconfigured") from error


def _prepare(db: Session, session_id: uuid.UUID) -> None:
    started = time.perf_counter()
    session = db.execute(
        select(InterviewSession).where(InterviewSession.id == session_id)
    ).scalar_one_or_none()
    if session is None:
        log_event("session.preparation_skipped", session_id=str(session_id), reason="not_found")
        return
    if session.status != SessionStatus.PREPARING_QUESTIONS:
        log_event(
            "session.preparation_skipped", session_id=str(session_id), status=session.status.value
        )
        return

    skills: list[_PlannedSkill] = []
    questions: list[_ValidQuestion] | None = None
    failure_code: str | None = None
    try:
        skills = _plan_skills(db, session)
        questions = _generate(_llm_client(), skills, list(session.snapshot or []))
    except _PlanInconsistent:
        failure_code = PLAN_INCONSISTENT
    except LLMInvalidOutput:
        failure_code = PLAN_INVALID
    except LLMUnavailable:
        failure_code = LLM_UNAVAILABLE

    # The session may have been cancelled or expired while the model was answering.
    locked = _lock_session(db, session_id)
    if locked is None or locked.status != SessionStatus.PREPARING_QUESTIONS:
        log_event(
            "session.preparation_discarded",
            session_id=str(session_id),
            status=locked.status.value if locked is not None else None,
            duration_ms=_duration_ms(started),
        )
        return

    locked.preparation_attempts = (locked.preparation_attempts or 0) + 1
    if questions is not None:
        _store_questions(db, locked, skills, questions)
        transition(locked, SessionStatus.IN_INTERVIEW)
    else:
        transition(locked, SessionStatus.PREPARATION_FAILED)
    db.flush()

    log_event(
        "session.preparation_finished",
        session_id=str(session_id),
        to=locked.status.value,
        failure_code=failure_code,
        questions=len(questions) if questions is not None else 0,
        without_source=sum(1 for skill in skills if not skill.sources),
        duration_ms=_duration_ms(started),
    )


def retry_preparation(db: Session, session: InterviewSession) -> None:
    """Queue question preparation again for a ``preparation_failed`` session (PLAN-92).

    Raises ``AppError`` ``INVALID_STATE`` (409) from any other state. Moves the session back to
    ``preparing_questions`` and enqueues ``session.prepare_questions``. The caller should pass
    a session locked by ``get_owned_session(..., for_update=True)`` and commits.
    """
    if session.status != SessionStatus.PREPARATION_FAILED:
        raise AppError.from_catalog(INVALID_STATE)
    transition(session, SessionStatus.PREPARING_QUESTIONS)
    touch_activity(session)
    db.flush()
    enqueue(db, PREPARE_QUESTIONS_JOB, {"session_id": str(session.id)})
    log_event("session.preparation_retried", session_id=str(session.id))
