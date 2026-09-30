"""Integration tests for the expiry of inactive interview sessions (INTV-13, CT-43)."""

import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import get_sessionmaker
from app.errors import AppError
from app.interviews.expiration import (
    EXPIRE_PERIODIC_NAME,
    EXPIRE_PERIODIC_SECONDS,
    expire_inactive_sessions,
    run_session_expiry,
)
from app.interviews.state_machine import (
    AWAITING_CANDIDATE_STATUSES,
    TERMINAL_STATUSES,
    transition,
)
from app.jobs.registry import default_registry
from app.models import User
from app.models.interview import InterviewSession, SessionStatus

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _session(db: Session, status: SessionStatus, idle: timedelta, now: datetime = NOW) -> uuid.UUID:
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    session = InterviewSession(user_id=user.id, status=status, last_activity_at=now - idle)
    db.add(session)
    db.flush()
    return session.id


def _status(db: Session, session_id: uuid.UUID) -> SessionStatus:
    statement = (
        select(InterviewSession.status)
        .where(InterviewSession.id == session_id)
        .execution_options(populate_existing=True)
    )
    return db.execute(statement).scalar_one()


def test_intv_13_in_interview_idle_31_days_expires_and_29_days_stays(db: Session) -> None:
    old = _session(db, SessionStatus.IN_INTERVIEW, timedelta(days=31))
    recent = _session(db, SessionStatus.IN_INTERVIEW, timedelta(days=29))

    expire_inactive_sessions(db, NOW)

    assert _status(db, old) is SessionStatus.EXPIRED
    assert _status(db, recent) is SessionStatus.IN_INTERVIEW


@pytest.mark.parametrize("status", sorted(AWAITING_CANDIDATE_STATUSES))
def test_intv_13_every_awaiting_status_expires(db: Session, status: SessionStatus) -> None:
    session_id = _session(db, status, timedelta(days=31))

    assert expire_inactive_sessions(db, NOW) == 1
    assert _status(db, session_id) is SessionStatus.EXPIRED


@pytest.mark.parametrize(
    "status",
    sorted(s for s in SessionStatus if s not in AWAITING_CANDIDATE_STATUSES),
)
def test_intv_13_non_awaiting_statuses_are_untouched(db: Session, status: SessionStatus) -> None:
    session_id = _session(db, status, timedelta(days=31))

    assert expire_inactive_sessions(db, NOW) == 0
    assert _status(db, session_id) is status


def test_intv_13_evaluating_idle_31_days_is_untouched(db: Session) -> None:
    session_id = _session(db, SessionStatus.EVALUATING, timedelta(days=31))

    assert expire_inactive_sessions(db, NOW) == 0
    assert _status(db, session_id) is SessionStatus.EVALUATING


def test_intv_13_returns_number_of_expired_sessions(db: Session) -> None:
    expired = [
        _session(db, SessionStatus.COLLECTING_REQUIREMENTS, timedelta(days=40)),
        _session(db, SessionStatus.AWAITING_CONFIRMATION, timedelta(days=31)),
        _session(db, SessionStatus.EVALUATION_FAILED, timedelta(days=30, seconds=1)),
    ]
    _session(db, SessionStatus.IN_INTERVIEW, timedelta(days=1))
    _session(db, SessionStatus.PREPARING_QUESTIONS, timedelta(days=60))

    assert expire_inactive_sessions(db, NOW) == 3
    assert all(_status(db, session_id) is SessionStatus.EXPIRED for session_id in expired)


def test_intv_13_exactly_30_days_is_not_expired(db: Session) -> None:
    session_id = _session(db, SessionStatus.IN_INTERVIEW, timedelta(days=30))

    assert expire_inactive_sessions(db, NOW) == 0
    assert _status(db, session_id) is SessionStatus.IN_INTERVIEW


def test_intv_13_second_run_is_idempotent(db: Session) -> None:
    session_id = _session(db, SessionStatus.IN_INTERVIEW, timedelta(days=31))

    assert expire_inactive_sessions(db, NOW) == 1
    assert expire_inactive_sessions(db, NOW) == 0
    assert _status(db, session_id) is SessionStatus.EXPIRED


def test_intv_13_expired_session_is_kept_without_completion(db: Session) -> None:
    session_id = _session(db, SessionStatus.IN_INTERVIEW, timedelta(days=31))

    expire_inactive_sessions(db, NOW)

    session = db.get(InterviewSession, session_id)
    assert session is not None
    assert session.status in TERMINAL_STATUSES
    assert session.completed_at is None


def test_intv_13_expiry_days_come_from_settings(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _session(db, SessionStatus.IN_INTERVIEW, timedelta(days=8))

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "session_expiry_days", 7)

    assert expire_inactive_sessions(db, NOW) == 1
    assert _status(db, session_id) is SessionStatus.EXPIRED


def test_intv_13_naive_datetime_is_rejected(db: Session) -> None:
    with pytest.raises(ValueError):
        expire_inactive_sessions(db, datetime(2026, 9, 30, 12, 0))  # noqa: DTZ001


def test_intv_13_registered_as_hourly_periodic() -> None:
    tasks = {task.name: task for task in default_registry.periodic_tasks()}

    task = tasks[EXPIRE_PERIODIC_NAME]
    assert EXPIRE_PERIODIC_NAME == "session.expire"
    assert EXPIRE_PERIODIC_SECONDS == 3600
    assert task.every_seconds == 3600
    assert task.fn is run_session_expiry


def test_intv_13_periodic_function_uses_current_time(db: Session) -> None:
    now = datetime.now(UTC)
    old = _session(db, SessionStatus.IN_INTERVIEW, timedelta(days=31), now=now)
    recent = _session(db, SessionStatus.IN_INTERVIEW, timedelta(days=1), now=now)

    run_session_expiry(db)

    assert _status(db, old) is SessionStatus.EXPIRED
    assert _status(db, recent) is SessionStatus.IN_INTERVIEW


# --- Concurrency: committed rows, real transactions ---------------------------------------


@pytest.fixture
def committed(migrated_database: str) -> Iterator[Callable[[SessionStatus, timedelta], uuid.UUID]]:
    """Create committed sessions (one user each); users and sessions are deleted at teardown."""
    user_ids: list[uuid.UUID] = []

    def create(status: SessionStatus, idle: timedelta) -> uuid.UUID:
        with get_sessionmaker()() as session:
            session_id = _session(session, status, idle, now=datetime.now(UTC))
            row = session.get(InterviewSession, session_id)
            assert row is not None
            user_ids.append(row.user_id)
            session.commit()
            return session_id

    yield create
    with get_sessionmaker()() as session:
        session.execute(delete(User).where(User.id.in_(user_ids)))
        session.commit()


def _committed_status(session_id: uuid.UUID) -> SessionStatus:
    with get_sessionmaker()() as session:
        return _status(session, session_id)


def _run_threads(workers: list[Callable[[], Any]]) -> tuple[list[Any], list[BaseException]]:
    barrier = threading.Barrier(len(workers))
    lock = threading.Lock()
    results: list[Any] = []
    errors: list[BaseException] = []

    def run(work: Callable[[], Any]) -> None:
        try:
            barrier.wait(timeout=10)
            value = work()
            with lock:
                results.append(value)
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertions
            with lock:
                errors.append(error)

    threads = [threading.Thread(target=run, args=(work,)) for work in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    return results, errors


def _periodic_run() -> int:
    with get_sessionmaker()() as session:
        count = expire_inactive_sessions(session, datetime.now(UTC))
        session.commit()
        return count


@pytest.mark.parametrize("attempt", range(3))
def test_intv_13_concurrent_periodic_runs_expire_each_session_once(
    committed: Callable[[SessionStatus, timedelta], uuid.UUID], attempt: int
) -> None:
    session_ids = [committed(SessionStatus.IN_INTERVIEW, timedelta(days=31)) for _ in range(5)]

    results, errors = _run_threads([_periodic_run for _ in range(3)])

    assert errors == []
    assert sum(results) == len(session_ids)
    assert all(_committed_status(sid) is SessionStatus.EXPIRED for sid in session_ids)


def test_intv_13_session_locked_by_user_action_is_skipped_and_stays_active(
    committed: Callable[[SessionStatus, timedelta], uuid.UUID],
) -> None:
    session_id = committed(SessionStatus.IN_INTERVIEW, timedelta(days=31))
    locked = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def user_action() -> None:
        try:
            with get_sessionmaker()() as session:
                row = session.execute(
                    select(InterviewSession)
                    .where(InterviewSession.id == session_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).scalar_one()
                locked.set()
                release.wait(timeout=10)
                row.last_activity_at = datetime.now(UTC)
                session.commit()
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertions
            errors.append(error)

    thread = threading.Thread(target=user_action)
    thread.start()
    try:
        assert locked.wait(timeout=10)
        # The periodic skips the row being answered instead of waiting on it.
        assert _periodic_run() == 0
    finally:
        release.set()
        thread.join(timeout=30)

    assert errors == []
    assert _committed_status(session_id) is SessionStatus.IN_INTERVIEW
    # The activity was recorded, so a later run does not expire it either.
    assert _periodic_run() == 0
    assert _committed_status(session_id) is SessionStatus.IN_INTERVIEW


def test_intv_13_user_action_after_expiry_sees_terminal_state(
    committed: Callable[[SessionStatus, timedelta], uuid.UUID],
) -> None:
    session_id = committed(SessionStatus.IN_INTERVIEW, timedelta(days=31))
    expired = threading.Event()
    release = threading.Event()
    outcome: list[BaseException | None] = []

    def user_action() -> None:
        expired.wait(timeout=10)
        with get_sessionmaker()() as session:
            row = session.execute(
                select(InterviewSession)
                .where(InterviewSession.id == session_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one()
            try:
                transition(row, SessionStatus.EVALUATING)
                session.commit()
                outcome.append(None)
            except AppError as error:
                session.rollback()
                outcome.append(error)

    thread = threading.Thread(target=user_action)
    thread.start()
    try:
        with get_sessionmaker()() as session:
            assert expire_inactive_sessions(session, datetime.now(UTC)) == 1
            expired.set()
            # The user action blocks on the row lock until the expiry commits.
            release.wait(timeout=0.3)
            session.commit()
    finally:
        expired.set()
        thread.join(timeout=30)

    assert len(outcome) == 1
    assert isinstance(outcome[0], AppError)
    assert _committed_status(session_id) is SessionStatus.EXPIRED
