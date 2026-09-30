"""Interview session lifecycle: start, ownership lookup, history, cancel (CT-36).

``start_session`` validates in a fixed order: language (``LANGUAGE_NOT_SUPPORTED``), level
(``VALIDATION_ERROR``), resume ownership (``RESOURCE_NOT_FOUND``) and status
(``RESUME_NOT_READY``) and, under a ``SELECT ... FOR UPDATE`` on the owner's ``users`` row,
the absence of another non-terminal session (``SESSION_IN_PROGRESS``, PLAN-02). A violation of
the partial unique index ``ux_one_open_session_per_user`` maps to the same error.

Lock order: ``users`` row, then ``interview_sessions`` rows. None of these functions commit
(CT-2): the caller commits.
"""

import copy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import (
    LANGUAGE_NOT_SUPPORTED,
    RESOURCE_NOT_FOUND,
    RESUME_NOT_READY,
    SESSION_IN_PROGRESS,
    VALIDATION_ERROR,
    AppError,
)
from app.interviews.state_machine import TERMINAL_STATUSES, transition
from app.logging_setup import log_event
from app.models.account import User
from app.models.interview import (
    ExpectedLevel,
    InterviewSession,
    Message,
    MessageKind,
    MessageRole,
    SessionStatus,
)
from app.models.resume import ResumeStatus
from app.resumes.service import get_owned_resume

__all__ = [
    "FIRST_ASSISTANT_MESSAGE",
    "cancel_session",
    "get_owned_session",
    "list_sessions",
    "start_session",
    "supported_languages",
    "touch_activity",
]

# Spec section 9: first message of every session (PLAN-01).
FIRST_ASSISTANT_MESSAGE = (
    "Please paste the job requirements for the position you are preparing for."
)

# Languages whose model validation was approved (LANG-01). LAC-08: English only in the MVP.
_SUPPORTED_LANGUAGES: tuple[str, ...] = ("en",)

_OPEN_SESSION_INDEX = "ux_one_open_session_per_user"


def supported_languages() -> list[str]:
    """Return the interview languages the candidate may choose (LANG-01)."""
    return list(_SUPPORTED_LANGUAGES)


def _parse_language(language: object) -> str:
    if not isinstance(language, str) or language not in _SUPPORTED_LANGUAGES:
        raise AppError.from_catalog(LANGUAGE_NOT_SUPPORTED)
    return language


def _parse_level(level: object) -> ExpectedLevel | None:
    if level is None or isinstance(level, ExpectedLevel):
        return level
    if isinstance(level, str):
        allowed = {member.value: member for member in ExpectedLevel}
        if level in allowed:
            return allowed[level]
    raise AppError.from_catalog(
        VALIDATION_ERROR, {"fields": [{"loc": ["interview_level"], "type": "enum"}]}
    )


def _parse_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _lock_owner(db: Session, user_id: UUID) -> None:
    """Serialize session starts of the same account on its ``users`` row (PLAN-02)."""
    locked = db.execute(
        select(User.id).where(User.id == user_id).with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise AppError.from_catalog(RESOURCE_NOT_FOUND)


def _find_open_session_id(db: Session, user_id: UUID) -> UUID | None:
    return db.execute(
        select(InterviewSession.id)
        .where(
            InterviewSession.user_id == user_id,
            InterviewSession.status.not_in(list(TERMINAL_STATUSES)),
        )
        .limit(1)
    ).scalar_one_or_none()


def _session_in_progress(session_id: UUID | None) -> AppError:
    details = {"session_id": str(session_id)} if session_id is not None else None
    return AppError.from_catalog(SESSION_IN_PROGRESS, details)


def _violates_open_session_index(exc: IntegrityError) -> bool:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == _OPEN_SESSION_INDEX


def _snapshot_of(extraction: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    # Deep copy: the session must never share mutable JSON with the resume (CV-12).
    return copy.deepcopy(extraction) if extraction else []


def start_session(
    db: Session,
    user: User,
    resume_id: UUID,
    language: str,
    interview_level: ExpectedLevel | None,
) -> InterviewSession:
    """Create a ``collecting_requirements`` session from a ``ready`` resume version.

    Freezes the version's extraction in ``snapshot`` (CV-12), copies its name and adds the
    first assistant message asking for the job requirements (PLAN-01). Raises ``AppError``
    with ``LANGUAGE_NOT_SUPPORTED``, ``VALIDATION_ERROR`` (unknown level),
    ``RESOURCE_NOT_FOUND``, ``RESUME_NOT_READY`` or ``SESSION_IN_PROGRESS`` (with
    ``details.session_id`` of the open session). The caller commits.
    """
    chosen_language = _parse_language(language)
    level = _parse_level(interview_level)
    resume = get_owned_resume(db, user, resume_id)
    if resume.status != ResumeStatus.READY:
        raise AppError.from_catalog(RESUME_NOT_READY)

    _lock_owner(db, user.id)
    open_id = _find_open_session_id(db, user.id)
    if open_id is not None:
        log_event("session.start_rejected", reason="session_in_progress")
        raise _session_in_progress(open_id)

    session = InterviewSession(
        user_id=user.id,
        resume_id=resume.id,
        resume_name=resume.filename,
        status=SessionStatus.COLLECTING_REQUIREMENTS,
        language=chosen_language,
        interview_level=level,
        snapshot=_snapshot_of(resume.extraction),
    )
    try:
        with db.begin_nested():
            db.add(session)
            db.flush()
    except IntegrityError as exc:
        if not _violates_open_session_index(exc):
            raise
        # A concurrent start won the partial unique index race (PLAN-02).
        log_event("session.start_rejected", reason="session_in_progress_concurrent")
        raise _session_in_progress(_find_open_session_id(db, user.id)) from None

    db.add(
        Message(
            session_id=session.id,
            role=MessageRole.ASSISTANT,
            kind=MessageKind.INFO,
            content=FIRST_ASSISTANT_MESSAGE,
        )
    )
    db.flush()
    db.refresh(session)
    log_event("session.started", session_id=str(session.id))
    return session


def get_owned_session(
    db: Session, user: User, session_id: UUID, for_update: bool = False
) -> InterviewSession:
    """Return a session owned by ``user``, optionally locked with ``SELECT ... FOR UPDATE``.

    Raises ``AppError`` ``RESOURCE_NOT_FOUND`` (404) for unknown ids, malformed ids and
    sessions of other users alike, so ownership is never disclosed.
    """
    parsed_id = _parse_uuid(session_id)
    if parsed_id is None:
        raise AppError.from_catalog(RESOURCE_NOT_FOUND)
    statement = select(InterviewSession).where(
        InterviewSession.id == parsed_id, InterviewSession.user_id == user.id
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    session = db.execute(statement).scalar_one_or_none()
    if session is None:
        raise AppError.from_catalog(RESOURCE_NOT_FOUND)
    return session


def list_sessions(db: Session, user: User) -> list[InterviewSession]:
    """Return every session of ``user`` in any state, newest first (DATA-01)."""
    statement = (
        select(InterviewSession)
        .where(InterviewSession.user_id == user.id)
        .order_by(InterviewSession.created_at.desc(), InterviewSession.id)
    )
    return list(db.execute(statement).scalars().all())


def cancel_session(db: Session, session: InterviewSession) -> None:
    """Move a non-terminal session to ``cancelled`` (INTV-12).

    Raises ``AppError`` ``INVALID_STATE`` (409) when the session is already terminal. The
    caller should pass a session locked by ``get_owned_session(..., for_update=True)``.
    """
    transition(session, SessionStatus.CANCELLED)
    touch_activity(session)
    db.flush()
    log_event("session.cancelled", session_id=str(session.id))


def touch_activity(session: InterviewSession) -> None:
    """Record candidate activity; idle sessions expire after 30 days (INTV-13)."""
    session.last_activity_at = datetime.now(UTC)
