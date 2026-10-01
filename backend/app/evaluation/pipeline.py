"""Evaluation pipeline job (CT-48; EVAL-01, EVAL-08, EVAL-11, EVAL-12, EVAL-13, EVAL-14,
EVAL-90, EVAL-93, DATA-91, KNOW-92).

``run_evaluation`` is the handler of the ``session.evaluate`` job queued by ``submit_answer``
when the last answer moves the session to ``evaluating``. It runs in the worker, so the
evaluation goes on whether or not the candidate is still logged in (EVAL-93).

Every answer without a stored evaluation is scored with ``evaluate_answer``; evaluations
already stored by a previous run are reused and never sent to the LLM again (EVAL-14). An
answer scored below 3 also gets its reference answer (EVAL-08); without it the item has no
valid evaluation and nothing is stored for it. An item whose evaluation stays invalid or
unavailable after the configured attempts never gets an artificial score (EVAL-13).

The LLM runs without any row lock. Afterwards the session row is locked with
``SELECT ... FOR UPDATE`` and its status re-checked: a session deleted or moved out of
``evaluating`` meanwhile gets nothing written (DATA-91). Under the lock:

- every item has a valid evaluation: the new evaluations are stored, the session moves to
  ``completed`` and the frozen report is stored, all in the worker transaction (EVAL-11);
- otherwise: the valid new evaluations are stored for a later retry and the session moves to
  ``evaluation_failed``, without report nor percentage (EVAL-13, KNOW-92).

A ``completed`` session is never evaluated again (EVAL-12). Nothing is committed here (CT-7).
An unexpected error marks the session ``evaluation_failed`` in a fresh session and is raised
again, so a session never stays ``evaluating`` once its job failed. Logs carry ids, counts and
codes only: never answers, justifications nor reference answers.
"""

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_sessionmaker
from app.errors import INVALID_STATE, AppError
from app.evaluation.evaluator import (
    EvaluationInput,
    EvaluationResult,
    EvaluationUnavailable,
    evaluate_answer,
)
from app.evaluation.reference_answers import ReferenceAnswer, build_reference_answer
from app.evaluation.scoring import compute_adherence
from app.interviews.answers import EVALUATE_JOB_KIND
from app.interviews.sessions import touch_activity
from app.interviews.state_machine import transition
from app.jobs.queue import enqueue
from app.llm.client import LLMClient, LLMUnavailable, get_llm_client
from app.llm.model_version import current_model_version
from app.models.assessment import Answer, Evaluation, Report
from app.models.interview import InterviewSession, Question, SessionStatus
from app.models.knowledge import SourceRef
from app.models.resume import ExtractionItem
from app.observability import log_event
from app.reports.builder import SATISFACTORY_MIN_SCORE, build_report_content

__all__ = [
    "ANSWER_MISSING",
    "EVALUATE_JOB_KIND",
    "EVALUATION_INVALID",
    "LLM_UNAVAILABLE",
    "UNEXPECTED_ERROR",
    "retry_evaluation",
    "run_evaluation",
]

# Failure codes, logged only (the session status is what the candidate sees).
ANSWER_MISSING = "ANSWER_MISSING"
EVALUATION_INVALID = "EVALUATION_INVALID"
LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
UNEXPECTED_ERROR = "UNEXPECTED_ERROR"

FAILURE_MARK_LOCK_TIMEOUT = "5s"

_FAILURE_CODES = {"invalid_output": EVALUATION_INVALID, "llm_unavailable": LLM_UNAVAILABLE}


@dataclass(frozen=True)
class _NewEvaluation:
    answer_id: uuid.UUID
    result: EvaluationResult
    reference: ReferenceAnswer | None


@dataclass
class _Outcome:
    new: list[_NewEvaluation]
    failure_codes: list[str]


class _LazyLLM:
    """Builds the LLM client on first use, so reused evaluations never need the server."""

    def __init__(self) -> None:
        self._client: LLMClient | None = None

    def get(self) -> LLMClient:
        if self._client is None:
            try:
                self._client = get_llm_client()
            except ValueError as error:
                # Misconfigured inference server (e.g. host outside llm_allowed_hosts).
                log_event("session.llm_client_unavailable", reason="invalid_configuration")
                raise EvaluationUnavailable("llm_unavailable") from error
        return self._client


def _parse_payload(payload: dict[str, str]) -> uuid.UUID:
    raw = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(raw, str):
        raise ValueError("session.evaluate payload requires a session_id")
    try:
        return uuid.UUID(raw)
    except ValueError:
        # The raw value is never echoed back.
        raise ValueError("session.evaluate payload has an invalid session_id") from None


def _duration_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


# --- reads -------------------------------------------------------------------------------------


def _questions(db: Session, session_id: uuid.UUID) -> list[Question]:
    statement = (
        select(Question).where(Question.session_id == session_id).order_by(Question.position)
    )
    return list(db.execute(statement).scalars().all())


def _answers(db: Session, session_id: uuid.UUID) -> list[Answer]:
    return list(db.execute(select(Answer).where(Answer.session_id == session_id)).scalars().all())


def _evaluations(db: Session, answer_ids: Iterable[uuid.UUID]) -> list[Evaluation]:
    ids = list(answer_ids)
    if not ids:
        return []
    statement = select(Evaluation).where(Evaluation.answer_id.in_(ids))
    return list(db.execute(statement).scalars().all())


def _ids(answers: Iterable[Answer]) -> list[uuid.UUID]:
    return [answer.id for answer in answers]


def _snapshot(session: InterviewSession) -> list[ExtractionItem]:
    items: list[ExtractionItem] = []
    for raw in session.snapshot or []:
        try:
            items.append(ExtractionItem.model_validate(raw))
        except ValidationError:
            # A malformed snapshot entry is never evidence for an example (EVAL-07).
            continue
    return items


def _evaluation_input(question: Question, answer: Answer) -> EvaluationInput:
    sources = (
        []
        if question.no_verified_source
        else [SourceRef.model_validate(raw) for raw in question.sources or []]
    )
    return EvaluationInput(
        question=question.text,
        skill=question.skill_name,
        expected_level=(
            question.expected_level.value if question.expected_level is not None else None
        ),
        reference_points=list(question.reference_points or []),
        sources=sources,
        answer=answer.content,
    )


# --- evaluation (no lock held) -----------------------------------------------------------------


def _evaluate_item(
    llm: _LazyLLM, inp: EvaluationInput, snapshot: list[ExtractionItem]
) -> tuple[EvaluationResult, ReferenceAnswer | None]:
    """Score one answer and, below 3, build its reference; raise ``EvaluationUnavailable``."""
    try:
        result = evaluate_answer(llm.get(), inp)
        reference = None
        if result.score < SATISFACTORY_MIN_SCORE:
            # An unsatisfactory item is only valid with its reference answer (EVAL-08).
            reference = build_reference_answer(llm.get(), inp, snapshot)
    except LLMUnavailable as error:
        raise EvaluationUnavailable("llm_unavailable") from error
    return result, reference


def _evaluate_missing(
    questions: list[Question],
    answers_by_question: dict[uuid.UUID, Answer],
    evaluated_answers: set[uuid.UUID],
    snapshot: list[ExtractionItem],
) -> _Outcome:
    outcome = _Outcome(new=[], failure_codes=[])
    llm = _LazyLLM()
    for question in questions:
        answer = answers_by_question.get(question.id)
        if answer is None:
            outcome.failure_codes.append(ANSWER_MISSING)
            continue
        if answer.id in evaluated_answers:
            continue  # Reused from a previous run (EVAL-14).
        try:
            result, reference = _evaluate_item(llm, _evaluation_input(question, answer), snapshot)
        except EvaluationUnavailable as error:
            outcome.failure_codes.append(_FAILURE_CODES[error.reason])
            continue
        outcome.new.append(_NewEvaluation(answer.id, result, reference))
    return outcome


# --- persistence (session row locked) ----------------------------------------------------------


def _lock_session(db: Session, session_id: uuid.UUID) -> InterviewSession | None:
    statement = (
        select(InterviewSession)
        .where(InterviewSession.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return db.execute(statement).scalar_one_or_none()


def _store_evaluations(
    db: Session, new: list[_NewEvaluation], already_stored: set[uuid.UUID], model_version: str
) -> list[Evaluation]:
    stored: list[Evaluation] = []
    for item in new:
        if item.answer_id in already_stored:
            continue  # Stored meanwhile by another run: keep the first one.
        evaluation = Evaluation(
            answer_id=item.answer_id,
            score=item.result.score,
            justification=item.result.justification,
            evidence_quotes=list(item.result.evidence_quotes),
            gap_explanation=item.result.gap_explanation,
            reference_answer=(
                item.reference.model_dump(mode="json") if item.reference is not None else None
            ),
            model_version=model_version,
        )
        db.add(evaluation)
        stored.append(evaluation)
    db.flush()
    return stored


def _complete(
    db: Session,
    session: InterviewSession,
    questions: list[Question],
    answers: list[Answer],
    evaluations: list[Evaluation],
) -> Report:
    settings = get_settings()
    model_version = current_model_version(settings)
    evaluation_by_answer = {evaluation.answer_id: evaluation for evaluation in evaluations}
    answer_by_question = {answer.question_id: answer for answer in answers}
    scores_by_skill: dict[str, list[int]] = {}
    for question in questions:
        evaluation = evaluation_by_answer[answer_by_question[question.id].id]
        scores_by_skill.setdefault(question.skill_name, []).append(evaluation.score)
    adherence = compute_adherence(scores_by_skill)

    # Before the builder, so the report freezes the session's completion time.
    transition(session, SessionStatus.COMPLETED)
    content = build_report_content(
        session,
        questions,
        answers,
        evaluations,
        adherence,
        model_version,
        settings.rubric_version,
    )
    report = Report(
        session_id=session.id,
        content=content.model_dump(mode="json"),
        adherence_percentage=adherence.percentage,
        model_version=model_version.id,
        rubric_version=settings.rubric_version,
    )
    db.add(report)
    db.flush()
    return report


def _fail_after_error(session_id: uuid.UUID, error: Exception) -> None:
    """Mark an evaluating session ``evaluation_failed`` in a fresh session (KNOW-92).

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
                    InterviewSession.status == SessionStatus.EVALUATING,
                )
                .values(status=SessionStatus.EVALUATION_FAILED)
            ).rowcount  # type: ignore[attr-defined]
            fresh.commit()
    except Exception as mark_error:
        log_event(
            "session.evaluation_failure_mark_failed",
            session_id=str(session_id),
            error_code=type(mark_error).__name__,
        )
        return
    if changed:
        log_event(
            "session.evaluation_failed",
            session_id=str(session_id),
            failure_code=UNEXPECTED_ERROR,
            error_code=type(error).__name__,
        )


# --- public API --------------------------------------------------------------------------------


def run_evaluation(db: Session, payload: dict[str, str]) -> None:
    """Job handler for ``session.evaluate``. Does not commit (CT-7).

    Only sessions in ``evaluating`` are processed; any other state (completed, failed,
    cancelled, unknown id) is skipped (EVAL-12). An unexpected error marks the session
    ``evaluation_failed`` in a fresh session and is raised again.
    """
    session_id = _parse_payload(payload)
    try:
        _run(db, session_id)
    except Exception as error:
        # Release any row lock held by the worker session before the fresh session writes.
        db.rollback()
        _fail_after_error(session_id, error)
        raise


def _run(db: Session, session_id: uuid.UUID) -> None:
    started = time.perf_counter()
    session = db.execute(
        select(InterviewSession).where(InterviewSession.id == session_id)
    ).scalar_one_or_none()
    if session is None:
        log_event("session.evaluation_skipped", session_id=str(session_id), reason="not_found")
        return
    if session.status != SessionStatus.EVALUATING:
        log_event(
            "session.evaluation_skipped", session_id=str(session_id), status=session.status.value
        )
        return

    questions = _questions(db, session_id)
    answers = _answers(db, session_id)
    answers_by_question = {answer.question_id: answer for answer in answers}
    reused = {evaluation.answer_id for evaluation in _evaluations(db, _ids(answers))}
    outcome = _evaluate_missing(questions, answers_by_question, reused, _snapshot(session))

    # The session may have been deleted, cancelled or handled by another run meanwhile.
    locked = _lock_session(db, session_id)
    if locked is None or locked.status != SessionStatus.EVALUATING:
        log_event(
            "session.evaluation_discarded",
            session_id=str(session_id),
            status=locked.status.value if locked is not None else None,
            duration_ms=_duration_ms(started),
        )
        return

    answers = _answers(db, session_id)
    existing = _evaluations(db, _ids(answers))
    model_version = current_model_version(get_settings()).id
    stored = _store_evaluations(
        db, outcome.new, {evaluation.answer_id for evaluation in existing}, model_version
    )
    evaluations = existing + stored
    locked.evaluation_attempts = (locked.evaluation_attempts or 0) + 1

    evaluated = {evaluation.answer_id for evaluation in evaluations}
    question_ids = {question.id for question in questions}
    answer_ids = {answer.id for answer in answers if answer.question_id in question_ids}
    # Every question needs an answer with a valid evaluation; never an artificial score.
    complete = (
        not outcome.failure_codes
        and bool(questions)
        and len(answer_ids) == len(questions)
        and answer_ids <= evaluated
    )
    if complete:
        _complete(db, locked, questions, answers, evaluations)
    else:
        transition(locked, SessionStatus.EVALUATION_FAILED)
    db.flush()

    log_event(
        "session.evaluation_finished",
        session_id=str(session_id),
        to=locked.status.value,
        failure_codes=",".join(sorted(set(outcome.failure_codes))) or None,
        reused=len(existing),
        evaluated=len(stored),
        failed=len(outcome.failure_codes),
        duration_ms=_duration_ms(started),
    )


def retry_evaluation(db: Session, session: InterviewSession) -> None:
    """Queue the evaluation again for an ``evaluation_failed`` session (EVAL-14).

    Raises ``AppError`` ``INVALID_STATE`` (409) from any other state. Moves the session back to
    ``evaluating`` and enqueues ``session.evaluate``; valid evaluations already stored are
    reused by the job. The caller should pass a session locked by
    ``get_owned_session(..., for_update=True)`` and commits.
    """
    if session.status != SessionStatus.EVALUATION_FAILED:
        raise AppError.from_catalog(INVALID_STATE)
    transition(session, SessionStatus.EVALUATING)
    touch_activity(session)
    db.flush()
    enqueue(db, EVALUATE_JOB_KIND, {"session_id": str(session.id)})
    log_event("session.evaluation_retried", session_id=str(session.id))
