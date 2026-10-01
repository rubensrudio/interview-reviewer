"""Job requirements structuring (CT-37, PLAN-03, PLAN-04, PLAN-06, PLAN-11, PLAN-15, PLAN-90,
PLAN-93, PLAN-94).

The candidate pastes the job requirements while the session is in ``collecting_requirements``
or ``awaiting_confirmation``. The text is checked deterministically first: blank text is
rejected with ``EMPTY_REQUIREMENTS`` and text that is not predominantly English gets an
assistant message asking for English, without changing the session status (LAC-08).

The text is then sent to the private LLM framed as untrusted content (DA-8), so instructions
embedded in the job posting are treated as data. The model answers with technical items and
non-technical requirements; its answer is post-validated without the model:

- a level outside junior / mid-level / senior / expert becomes null (LAC-22);
- a composite name joining independent skills with ``/`` or `` and `` becomes one item per
  skill (PLAN-11), except for well-known single concepts such as ``CI/CD``;
- an ambiguous item is kept as ``pending_clarification`` and its question is posted in the
  chat (PLAN-04);
- every string is sanitized (NUL, control characters, lone surrogates) and length-capped.

The LLM runs synchronously (PR-4) before any change: when it stays unavailable or invalid
after ``llm_max_attempts`` attempts, ``LLM_UNAVAILABLE`` (503) is raised and the session is
untouched. After the LLM call the session row is locked with ``SELECT ... FOR UPDATE`` and its
status checked again. Nothing is committed here: the caller commits (CT-2). Neither the text,
the prompts nor the model answer are logged; only counts are.
"""

import re
import uuid
from datetime import timedelta

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import EMPTY_REQUIREMENTS, INVALID_STATE, LLM_UNAVAILABLE, AppError
from app.interviews.sessions import touch_activity
from app.interviews.state_machine import transition
from app.llm.client import LLMClient, LLMInvalidOutput, LLMUnavailable, run_with_attempts
from app.llm.untrusted import UNTRUSTED_RULES, wrap_untrusted
from app.models.interview import (
    ExpectedLevel,
    InterviewSession,
    Message,
    MessageKind,
    MessageRole,
    RequirementItem,
    SessionStatus,
)
from app.observability import log_event
from app.resumes.language import is_predominantly_english

__all__ = [
    "ACCEPTING_STATUSES",
    "MAX_REQUIREMENTS_CHARS",
    "NO_TECHNICAL_SKILLS_MESSAGE",
    "NOT_ENGLISH_MESSAGE",
    "REQUIREMENTS_STRUCTURED_MESSAGE",
    "REQUIREMENTS_TASK",
    "structure_requirements",
]

REQUIREMENTS_TASK = "requirements_structuring"

# Statuses in which the candidate may (re)send the job requirements.
ACCEPTING_STATUSES: frozenset[SessionStatus] = frozenset(
    {SessionStatus.COLLECTING_REQUIREMENTS, SessionStatus.AWAITING_CONFIRMATION}
)

# Spec section 9 (PLAN-15).
NOT_ENGLISH_MESSAGE = (
    "Please send the job requirements in English. Other languages are not supported yet."
)
# PLAN-94: no technical skill identified in the text.
NO_TECHNICAL_SKILLS_MESSAGE = (
    "No technical skills were found in the job requirements. "
    "Define at least one required technical skill to continue."
)
REQUIREMENTS_STRUCTURED_MESSAGE = (
    "Here is the structured list of the job requirements. Review it and confirm it to continue."
)
_DEFAULT_CLARIFICATION = (
    'Could you clarify the requirement "{name}"? Tell me whether it is required or nice to '
    "have and what it means for this position."
)

# Upper bounds for untrusted input and model output.
MAX_REQUIREMENTS_CHARS = 20_000
_MAX_ITEMS = 100
_MAX_NON_TECHNICAL = 50
_MAX_NAME_CHARS = 200
_MAX_TERMS_PER_ITEM = 10
_MAX_QUESTION_CHARS = 500

_UNTRUSTED_LABEL = "job_requirements"

_WHITESPACE_RE = re.compile(r"\s+")
# NUL is dropped (PostgreSQL text cannot hold it); other C0/C1 controls and lone surrogates
# (which cannot be encoded as UTF-8) become spaces.
_NUL_RE = re.compile("\x00")
_CONTROL_RE = re.compile("[\x01-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
_COMPOSITE_RE = re.compile(r"\s*/\s*|\s+and\s+", re.IGNORECASE)
# Names written with "/" or "and" that denote a single concept, not independent skills.
_SINGLE_CONCEPTS = frozenset(
    {"ci/cd", "tcp/ip", "pl/sql", "ui/ux", "i/o", "a/b testing", "r&d", "m&a"}
)
_LEVEL_ALIASES: dict[str, ExpectedLevel] = {
    "junior": ExpectedLevel.JUNIOR,
    "mid-level": ExpectedLevel.MID_LEVEL,
    "mid level": ExpectedLevel.MID_LEVEL,
    "midlevel": ExpectedLevel.MID_LEVEL,
    "senior": ExpectedLevel.SENIOR,
    "expert": ExpectedLevel.EXPERT,
}

_SYSTEM_PROMPT = (
    "You structure the requirements of a job posting for a technical interview. Return JSON "
    'with the keys "items" and "non_technical".\n'
    '- "items" lists the technical skills (technologies, languages, tools, practices). Each '
    'item has the keys "name", "original_terms", "classification", "level", "ambiguous" and '
    '"clarification_question".\n'
    '- "name" is the skill name. "original_terms" lists the terms of the posting that refer '
    "to this skill, copied as written; group synonyms of the same skill into one item with "
    "all their terms. Never join independent skills into one item.\n"
    '- "classification" is "required" or "nice_to_have".\n'
    '- "level" is "junior", "mid-level", "senior" or "expert" only when the posting states '
    "the expected level for the skill; otherwise null.\n"
    '- "ambiguous" is true when it is unclear whether the skill is required or what it '
    'means; then "clarification_question" is a short question to the candidate. Otherwise '
    '"ambiguous" is false and "clarification_question" is null.\n'
    '- "non_technical" lists the non-technical requirements (soft skills, spoken languages, '
    'years of experience, education), each as a short text. Never put them in "items".\n'
    "Only use requirements stated in the posting. Do not invent skills.\n\n" + UNTRUSTED_RULES
)


class _LLMRequirementItem(BaseModel):
    """One technical item as answered by the model; validated afterwards."""

    model_config = ConfigDict(extra="forbid")

    name: str
    original_terms: list[str]
    classification: str
    level: str | None = None
    ambiguous: bool = False
    clarification_question: str | None = None


class _LLMRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[_LLMRequirementItem]
    non_technical: list[str]


def _sanitize(text: str) -> str:
    return _CONTROL_RE.sub(" ", _NUL_RE.sub("", text))


def _clean(text: str | None, limit: int) -> str:
    if text is None:
        return ""
    return _WHITESPACE_RE.sub(" ", _sanitize(text)).strip()[:limit].strip()


def _parse_level(level: str | None) -> ExpectedLevel | None:
    if level is None:
        return None
    key = _WHITESPACE_RE.sub(" ", level.replace("_", " ")).strip().lower()
    return _LEVEL_ALIASES.get(key)


def _split_composite(name: str) -> list[str]:
    """Split ``"Docker/Kubernetes"`` or ``"Docker and Kubernetes"`` into independent names."""
    if name.lower() in _SINGLE_CONCEPTS:
        return [name]
    parts = [part.strip() for part in _COMPOSITE_RE.split(name)]
    parts = [part for part in parts if part]
    return parts if len(parts) > 1 else [name]


def _clean_terms(terms: list[str], fallback: str) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for term in terms:
        value = _clean(term, _MAX_NAME_CHARS)
        if value and value.lower() not in seen:
            seen.add(value.lower())
            cleaned.append(value)
        if len(cleaned) == _MAX_TERMS_PER_ITEM:
            break
    return cleaned or [fallback]


def _to_items(answer: _LLMRequirements) -> list[RequirementItem]:
    """Post-validate the model items: level scale, composite split, ambiguity, dedupe."""
    by_name: dict[str, RequirementItem] = {}
    for candidate in answer.items:
        name = _clean(candidate.name, _MAX_NAME_CHARS)
        if not name or candidate.classification not in ("required", "nice_to_have"):
            continue
        level = _parse_level(candidate.level)
        question = _clean(candidate.clarification_question, _MAX_QUESTION_CHARS)
        parts = _split_composite(name)
        for part in parts:
            # A split part only keeps its own term: the composite term belongs to no single part.
            terms = [part] if len(parts) > 1 else _clean_terms(candidate.original_terms, part)
            item = RequirementItem(
                id=uuid.uuid4().hex,
                name=part,
                original_terms=terms,
                classification="required"
                if candidate.classification == "required"
                else "nice_to_have",
                level=level,
                pending_clarification=candidate.ambiguous,
                clarification_question=(
                    (question or _DEFAULT_CLARIFICATION.format(name=part))
                    if candidate.ambiguous
                    else None
                ),
            )
            existing = by_name.get(part.lower())
            by_name[part.lower()] = item if existing is None else _merge(existing, item)
        if len(by_name) >= _MAX_ITEMS:
            break
    return list(by_name.values())[:_MAX_ITEMS]


def _merge(first: RequirementItem, second: RequirementItem) -> RequirementItem:
    """Merge two items with the same name: required wins and pending stays pending."""
    terms = _clean_terms([*first.original_terms, *second.original_terms], first.name)
    pending = first.pending_clarification or second.pending_clarification
    return first.model_copy(
        update={
            "original_terms": terms,
            "classification": "required"
            if "required" in (first.classification, second.classification)
            else "nice_to_have",
            "level": first.level or second.level,
            "pending_clarification": pending,
            "clarification_question": (
                first.clarification_question or second.clarification_question
            )
            if pending
            else None,
        }
    )


def _to_non_technical(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean(value, _MAX_NAME_CHARS)
        if text and text.lower() not in seen:
            seen.add(text.lower())
            cleaned.append(text)
        if len(cleaned) == _MAX_NON_TECHNICAL:
            break
    return cleaned


def _ensure_accepting(session: InterviewSession) -> None:
    if session.status not in ACCEPTING_STATUSES:
        raise AppError.from_catalog(INVALID_STATE)


def _lock_session(db: Session, session: InterviewSession) -> None:
    """Re-read the session row under ``FOR UPDATE`` (no-op if the caller already holds it)."""
    db.execute(
        select(InterviewSession)
        .where(InterviewSession.id == session.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()


class _Chat:
    """Adds the chat messages of one request in a stable order.

    ``now()`` is fixed for the whole transaction, so each message gets one more microsecond
    to keep the candidate message before the assistant replies (views order by created_at).
    """

    def __init__(self, db: Session, session: InterviewSession) -> None:
        self._db = db
        self._session_id = session.id
        self._count = 0

    def add(self, role: MessageRole, kind: MessageKind, content: str) -> None:
        self._count += 1
        self._db.add(
            Message(
                session_id=self._session_id,
                role=role,
                kind=kind,
                content=content,
                created_at=func.now() + timedelta(microseconds=self._count),
            )
        )


def _ask_llm(llm: LLMClient, text: str) -> _LLMRequirements:
    user_prompt = "Structure the job requirements below.\n\n" + wrap_untrusted(
        _UNTRUSTED_LABEL, text
    )
    attempts = get_settings().llm_max_attempts
    try:
        return run_with_attempts(
            lambda: llm.complete_structured(
                REQUIREMENTS_TASK, _SYSTEM_PROMPT, user_prompt, _LLMRequirements
            ),
            attempts=attempts,
            retry_on=(LLMInvalidOutput, LLMUnavailable),
        )
    except LLMInvalidOutput as error:
        log_event("requirements.structuring_failed", reason="invalid_output", attempts=attempts)
        raise AppError.from_catalog(LLM_UNAVAILABLE) from error
    except LLMUnavailable as error:
        log_event("requirements.structuring_failed", reason="llm_unavailable", attempts=attempts)
        raise AppError.from_catalog(LLM_UNAVAILABLE) from error


def structure_requirements(
    db: Session, llm: LLMClient, session: InterviewSession, text: str
) -> None:
    """Structure the job requirements ``text`` into the session's requirement list.

    Raises ``AppError`` ``INVALID_STATE`` (409) outside ``collecting_requirements`` and
    ``awaiting_confirmation``, ``EMPTY_REQUIREMENTS`` (422) for blank text and
    ``LLM_UNAVAILABLE`` (503) when the model fails; none of them change the session. Text not
    predominantly in English only adds the chat messages (PLAN-15). Otherwise the list and the
    non-technical requirements are stored and the session moves to ``awaiting_confirmation``.
    The caller should pass a session locked by ``get_owned_session(..., for_update=True)`` and
    commits.
    """
    _ensure_accepting(session)
    raw = text if isinstance(text, str) else ""
    clean_text = _sanitize(raw).strip()[:MAX_REQUIREMENTS_CHARS]
    if not clean_text:
        raise AppError.from_catalog(EMPTY_REQUIREMENTS)

    if not is_predominantly_english(clean_text):
        _lock_session(db, session)
        _ensure_accepting(session)
        chat = _Chat(db, session)
        chat.add(MessageRole.CANDIDATE, MessageKind.REQUIREMENTS, clean_text)
        chat.add(MessageRole.ASSISTANT, MessageKind.REQUIREMENTS_REPLY, NOT_ENGLISH_MESSAGE)
        touch_activity(session)
        db.flush()
        log_event("requirements.rejected", session_id=str(session.id), reason="not_english")
        return

    answer = _ask_llm(llm, clean_text)
    items = _to_items(answer)
    non_technical = _to_non_technical(answer.non_technical)

    # The LLM call may take a while: lock and re-check before writing (PR-4).
    _lock_session(db, session)
    _ensure_accepting(session)
    transition(session, SessionStatus.AWAITING_CONFIRMATION)
    session.requirements_text = clean_text
    session.requirement_items = [item.model_dump(mode="json") for item in items]
    session.non_technical = non_technical
    touch_activity(session)

    chat = _Chat(db, session)
    chat.add(MessageRole.CANDIDATE, MessageKind.REQUIREMENTS, clean_text)
    reply = REQUIREMENTS_STRUCTURED_MESSAGE if items else NO_TECHNICAL_SKILLS_MESSAGE
    chat.add(MessageRole.ASSISTANT, MessageKind.REQUIREMENTS_REPLY, reply)
    for item in items:
        if item.pending_clarification and item.clarification_question:
            chat.add(
                MessageRole.ASSISTANT,
                MessageKind.CLARIFICATION_REQUEST,
                item.clarification_question,
            )
    db.flush()
    log_event(
        "requirements.structured",
        session_id=str(session.id),
        items=len(items),
        pending=sum(1 for item in items if item.pending_clarification),
        non_technical=len(non_technical),
    )
