"""Integration tests for the job lock heartbeat (LAC-44, CT-6 ``renew_lock``).

``STALE_LOCK_TIMEOUT`` and the heartbeat interval are shortened with ``monkeypatch`` so a
handler can outlive the stale timeout in a few seconds. The worker commits for real, so every
test uses a unique job kind and deletes its jobs afterwards.
"""

import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.db import get_engine
from app.jobs import worker as worker_module
from app.jobs.queue import claim_next, enqueue, renew_lock
from app.jobs.registry import JobRegistry
from app.jobs.worker import Worker, reap_stale_jobs
from app.models.job import Job, JobStatus

SHORT_STALE = timedelta(seconds=1)
SHORT_HEARTBEAT_SECONDS = 0.2
HEARTBEAT_THREAD_PREFIX = "job-heartbeat"


@pytest.fixture
def job_kinds(migrated_database: str) -> Iterator[list[str]]:
    kinds: list[str] = []
    yield kinds
    with Session(get_engine()) as session:
        session.execute(delete(Job).where(Job.kind.in_(kinds)))
        session.commit()


@pytest.fixture
def short_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "STALE_LOCK_TIMEOUT", SHORT_STALE)
    monkeypatch.setattr(worker_module, "HEARTBEAT_INTERVAL_SECONDS", SHORT_HEARTBEAT_SECONDS)


def _unique_kind(kinds: list[str]) -> str:
    kind = f"test.heartbeat.{uuid.uuid4().hex[:8]}"
    kinds.append(kind)
    return kind


def _enqueue_committed(kind: str) -> uuid.UUID:
    with Session(get_engine()) as session:
        job_id = enqueue(session, kind, {"n": "1"})
        session.commit()
    return job_id


def _load(job_id: uuid.UUID) -> Job:
    with Session(get_engine(), expire_on_commit=False) as session:
        return session.execute(select(Job).where(Job.id == job_id)).scalar_one()


def _reap_committed() -> int:
    with Session(get_engine()) as session:
        reaped = reap_stale_jobs(session, SHORT_STALE)
        session.commit()
    return reaped


def _heartbeat_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(HEARTBEAT_THREAD_PREFIX)]


def _worker(registry: JobRegistry) -> Worker:
    # The test drives the reaper itself; the worker's own reaper stays off.
    return Worker(registry=registry, reap_every_seconds=None)


def test_eval_93_long_job_with_heartbeat_is_not_reaped_and_finishes_done(
    job_kinds: list[str], short_timeouts: None
) -> None:
    kind = _unique_kind(job_kinds)
    registry = JobRegistry()
    calls: list[str] = []

    def handler(db: Session, payload: dict[str, str]) -> None:
        calls.append(payload["n"])
        # Run well past the (shortened) stale timeout while a reaper keeps sweeping.
        deadline = time.monotonic() + 3 * SHORT_STALE.total_seconds()
        while time.monotonic() < deadline:
            _reap_committed()
            time.sleep(SHORT_HEARTBEAT_SECONDS)

    registry.register(kind, handler)
    job_id = _enqueue_committed(kind)

    assert _worker(registry).run_once() is True

    job = _load(job_id)
    assert calls == ["1"]
    assert job.status is JobStatus.DONE
    assert job.attempts == 1
    assert job.last_error_code is None
    assert _heartbeat_threads() == []


def test_know_92_job_of_dead_worker_is_still_reaped_while_heartbeat_runs(
    job_kinds: list[str], short_timeouts: None
) -> None:
    dead_kind = _unique_kind(job_kinds)
    live_kind = _unique_kind(job_kinds)
    dead_id = _enqueue_committed(dead_kind)
    # A worker claims the job and dies: nobody renews its lock.
    with Session(get_engine()) as session:
        claimed = claim_next(session)
        assert claimed is not None and claimed.id == dead_id
        session.commit()

    registry = JobRegistry()
    reaped_total: list[int] = []

    def live_handler(db: Session, payload: dict[str, str]) -> None:
        deadline = time.monotonic() + 3 * SHORT_STALE.total_seconds()
        while time.monotonic() < deadline:
            reaped_total.append(_reap_committed())
            time.sleep(SHORT_HEARTBEAT_SECONDS)

    registry.register(live_kind, live_handler)
    live_id = _enqueue_committed(live_kind)

    _worker(registry).run_once()

    dead = _load(dead_id)
    assert dead.status is JobStatus.QUEUED
    assert dead.last_error_code == "StaleJobLock"
    assert dead.attempts == 1
    assert sum(reaped_total) >= 1
    assert _load(live_id).status is JobStatus.DONE


def test_renew_lock_is_fenced_by_attempts_and_status(job_kinds: list[str]) -> None:
    kind = _unique_kind(job_kinds)
    job_id = _enqueue_committed(kind)
    with Session(get_engine()) as session:
        job = claim_next(session)
        assert job is not None and job.id == job_id
        session.commit()
    past = datetime.now(UTC) - timedelta(minutes=10)
    with Session(get_engine()) as session:
        session.execute(update(Job).where(Job.id == job_id).values(locked_at=past))
        session.commit()

    with Session(get_engine()) as session:
        assert renew_lock(session, job_id, attempts=2) is False
        assert renew_lock(session, uuid.uuid4(), attempts=1) is False
        session.commit()
    assert _load(job_id).locked_at == past

    with Session(get_engine()) as session:
        assert renew_lock(session, job_id, attempts=1) is True
        session.commit()
    renewed = _load(job_id).locked_at
    assert renewed is not None and renewed > past

    with Session(get_engine()) as session:
        session.execute(update(Job).where(Job.id == job_id).values(status=JobStatus.DONE))
        session.commit()
    with Session(get_engine()) as session:
        assert renew_lock(session, job_id, attempts=1) is False
        session.commit()


def test_heartbeat_does_not_renew_lock_of_job_that_changed_owner(
    job_kinds: list[str], short_timeouts: None, caplog: pytest.LogCaptureFixture
) -> None:
    kind = _unique_kind(job_kinds)
    registry = JobRegistry()
    other_owner_lock = datetime.now(UTC) - timedelta(minutes=10)

    def handler(db: Session, payload: dict[str, str]) -> None:
        # Simulate: the job was reaped and claimed again by another worker (new attempt).
        with Session(get_engine()) as other:
            other.execute(
                update(Job)
                .where(Job.kind == kind)
                .values(attempts=Job.attempts + 1, locked_at=other_owner_lock)
            )
            other.commit()
        time.sleep(5 * SHORT_HEARTBEAT_SECONDS)

    registry.register(kind, handler)
    job_id = _enqueue_committed(kind)
    caplog.set_level("INFO")

    _worker(registry).run_once()

    job = _load(job_id)
    assert job.status is JobStatus.RUNNING
    assert job.attempts == 2
    assert job.locked_at == other_owner_lock
    events = [record.getMessage() for record in caplog.records]
    assert events.count("job.lock_lost") == 1
    assert "job.ownership_lost" in events
    assert _heartbeat_threads() == []


@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyboardInterrupt()])
def test_heartbeat_stops_when_handler_raises(
    job_kinds: list[str], short_timeouts: None, error: BaseException
) -> None:
    kind = _unique_kind(job_kinds)
    registry = JobRegistry()

    def handler(db: Session, payload: dict[str, str]) -> None:
        time.sleep(2 * SHORT_HEARTBEAT_SECONDS)
        raise error

    registry.register(kind, handler)
    _enqueue_committed(kind)

    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            _worker(registry).run_once()
    else:
        _worker(registry).run_once()

    assert _heartbeat_threads() == []


def test_heartbeat_interval_must_be_shorter_than_stale_timeout() -> None:
    with pytest.raises(ValueError):
        Worker(stale_after=timedelta(seconds=10), heartbeat_every_seconds=10)
    with pytest.raises(ValueError):
        Worker(heartbeat_every_seconds=0)
