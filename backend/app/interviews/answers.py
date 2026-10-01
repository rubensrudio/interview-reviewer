"""Idempotent answer submission during the interview (CT-41).

``submit_answer`` locks the session row (``SELECT ... FOR UPDATE``) before reading anything
else, so concurrent submissions for the same session are serialized. Checks run in a fixed
order:

1. the same ``idempotency_key`` was already recorded for the session: return, no effect
   (INTV-06);
2. terminal status: ``SESSION_CLOSED`` (INTV-93); any other status but ``in_interview``:
   ``INVALID_STATE``;
3. empty or blank answer: ``EMPTY_ANSWER`` (INTV-04); longer than ``max_answer_chars``:
   ``ANSWER_TOO_LONG`` with ``details.limit`` (INTV-05);
4. question unknown or of another session: ``RESOURCE_NOT_FOUND``; already answered:
   ``QUESTION_ALREADY_ANSWERED`` (INTV-09, INTV-90); not the current question:
   ``NOT_CURRENT_QUESTION`` (INTV-91).

Any non-empty text is accepted, including "I don't know" (INTV-07). The answer is never
evaluated here. Lock order: ``interview_sessions`` row, then ``answers``/``questions``.
Nothing is committed (CT-2): the caller commits.
"""

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import (
    ANSWER_TOO_LONG,
    EMPTY_ANSWER,
    INVALID_STATE,
    NOT_CURRENT_QUESTION,
    QUESTION_ALREADY_ANSWERED,
    RESOURCE_NOT_FOUND,
    SESSION_CLOSED,
    VALIDATION_ERROR,
    AppError,
)
from app.interviews.sessions import touch_activity
from app.interviews.state_machine import TERMINAL_STATUSES, transition
from app.jobs.queue import enqueue
from app.models.assessment import Answer
from app.models.interview import InterviewSession, Question, SessionStatus
from app.observability import log_event

__all__ = ["EVALUATE_JOB_KIND", "IDEMPOTENCY_KEY_MAX_LENGTH", "submit_answer"]

EVALUATE_JOB_KIND = "session.evaluate"

# Matches `answers.idempotency_key varchar(64)`.
IDEMPOTENCY_KEY_MAX_LENGTH = 64

# NUL cannot be stored in PostgreSQL text and lone surrogates cannot be encoded as UTF-8.
_UNSTORABLE_RE = re.compile("[\x00\ud800-\udfff]")

# Unique constraints of `answers` (migration 0006).
_QUESTION_UNIQUE = "uq_answers_question_id"
_IDEMPOTENCY_UNIQUE = "uq_answers_session_id"


def _validate_idempotency_key(key: object) -> str:
    if not isinstance(key, str) or not key or len(key) > IDEMPOTENCY_KEY_MAX_LENGTH:
        raise AppError.from_catalog(
            VALIDATION_ERROR, {"fields": [{"loc": ["idempotency_key"], "type": "string"}]}
        )
    return key


def _clean_content(content: object) -> str:
    if not isinstance(content, str):
        raise AppError.from_catalog(EMPTY_ANSWER)
    cleaned = _UNSTORABLE_RE.sub("", content)
    if not cleaned.strip():
        raise AppError.from_catalog(EMPTY_ANSWER)
    limit = get_settings().max_answer_chars
    if len(cleaned) > limit:
        raise AppError.from_catalog(ANSWER_TOO_LONG, {"limit": limit})
    return cleaned


def _parse_question_id(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            pass
    raise AppError.from_catalog(RESOURCE_NOT_FOUND)


def _lock_session(db: Session, session: InterviewSession) -> InterviewSession:
    locked = db.execute(
        select(InterviewSession)
        .where(InterviewSession.id == session.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if locked is None:
        raise AppError.from_catalog(RESOURCE_NOT_FOUND)
    return locked


def _already_recorded(db: Session, session_id: UUID, key: str) -> bool:
    found = db.execute(
        select(Answer.id).where(Answer.session_id == session_id, Answer.idempotency_key == key)
    ).scalar_one_or_none()
    return found is not None


def _check_status(session: InterviewSession) -> None:
    if session.status in TERMINAL_STATUSES:
        raise AppError.from_catalog(SESSION_CLOSED)
    if session.status != SessionStatus.IN_INTERVIEW:
        raise AppError.from_catalog(INVALID_STATE)


def _check_question(db: Session, session_id: UUID, question_id: UUID) -> None:
    question_session = db.execute(
        select(Question.session_id).where(Question.id == question_id)
    ).scalar_one_or_none()
    # The database does not tie an answer's question to its session: enforce it here.
    if question_session != session_id:
        raise AppError.from_catalog(RESOURCE_NOT_FOUND)
    answered = db.execute(
        select(Answer.id).where(Answer.question_id == question_id)
    ).scalar_one_or_none()
    if answered is not None:
        raise AppError.from_catalog(QUESTION_ALREADY_ANSWERED)
    current_id = db.execute(
        select(Question.id)
        .where(
            Question.session_id == session_id,
            ~select(Answer.id).where(Answer.question_id == Question.id).exists(),
        )
        .order_by(Question.position)
        .limit(1)
    ).scalar_one_or_none()
    if current_id != question_id:
        raise AppError.from_catalog(NOT_CURRENT_QUESTION)


def _violated_constraint(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


def _insert_answer(db: Session, answer: Answer) -> bool:
    """Insert ``answer`` in a savepoint; return ``False`` when it replays an earlier one."""
    try:
        with db.begin_nested():
            db.add(answer)
            db.flush()
    except IntegrityError as exc:
        constraint = _violated_constraint(exc)
        if constraint == _IDEMPOTENCY_UNIQUE:
            return False
        if constraint == _QUESTION_UNIQUE:
            raise AppError.from_catalog(QUESTION_ALREADY_ANSWERED) from None
        raise
    return True


def submit_answer(
    db: Session,
    session: InterviewSession,
    question_id: UUID,
    content: str,
    idempotency_key: str,
) -> None:
    """Record the candidate's answer to the current question of ``session`` (CT-41).

    Adds exactly one to ``answered_count`` and records activity. When every planned question
    is answered, moves the session to ``evaluating`` and enqueues ``session.evaluate``
    (INTV-11). A resubmission with the same ``idempotency_key`` has no effect. Raises
    ``AppError`` as described in the module docstring. The caller commits.
    """
    key = _validate_idempotency_key(idempotency_key)
    locked = _lock_session(db, session)

    if _already_recorded(db, locked.id, key):
        log_event("answer.replayed", session_id=str(locked.id))
        return

    _check_status(locked)
    text = _clean_content(content)
    parsed_question_id = _parse_question_id(question_id)
    _check_question(db, locked.id, parsed_question_id)

    answer = Answer(
        session_id=locked.id, question_id=parsed_question_id, content=text, idempotency_key=key
    )
    if not _insert_answer(db, answer):
        log_event("answer.replayed", session_id=str(locked.id))
        return

    locked.answered_count += 1
    touch_activity(locked)
    finished = locked.planned_count is not None and locked.answered_count >= locked.planned_count
    if finished:
        transition(locked, SessionStatus.EVALUATING)
        enqueue(db, EVALUATE_JOB_KIND, {"session_id": str(locked.id)})
    db.flush()
    log_event(
        "answer.accepted",
        session_id=str(locked.id),
        answered_count=locked.answered_count,
        finished=finished,
    )
