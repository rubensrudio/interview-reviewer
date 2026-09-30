"""Integration tests for the job worker, handler registry and stale-job reaper (CT-7, LAC-38).

The worker opens its own sessions and commits for real, so every test uses a unique job kind
and deletes its jobs afterwards.
"""

import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.db import get_engine
from app.jobs import registry as registry_module
from app.jobs.queue import enqueue
from app.jobs.registry import JobRegistry
from app.jobs.worker import (
    RetryableJobError,
    Worker,
    reap_stale_jobs,
    register,
    register_periodic,
)
from app.models.job import Job, JobStatus


@pytest.fixture
def job_kinds(migrated_database: str) -> Iterator[list[str]]:
    """Kinds created by a test; their jobs are deleted at teardown."""
    kinds: list[str] = []
    yield kinds
    with Session(get_engine()) as session:
        session.execute(delete(Job).where(Job.kind.in_(kinds)))
        session.commit()


def _unique_kind(kinds: list[str], prefix: str = "test.worker") -> str:
    kind = f"{prefix}.{uuid.uuid4().hex[:8]}"
    kinds.append(kind)
    return kind


def _enqueue_committed(kind: str, payload: dict[str, str] | None = None) -> uuid.UUID:
    with Session(get_engine()) as session:
        job_id = enqueue(session, kind, payload or {"n": "1"})
        session.commit()
    return job_id


def _load(job_id: uuid.UUID) -> Job:
    with Session(get_engine(), expire_on_commit=False) as session:
        return session.execute(select(Job).where(Job.id == job_id)).scalar_one()


def _run_until_attempted(worker: Worker, job_id: uuid.UUID, max_rounds: int = 20) -> Job:
    """Run the worker until the given job was claimed at least once and is not running."""
    for _ in range(max_rounds):
        worker.run_once()
        job = _load(job_id)
        if job.attempts >= 1 and job.status is not JobStatus.RUNNING:
            return job
    raise AssertionError("job was not processed")


def _no_op_worker(registry: JobRegistry) -> Worker:
    # Reaper disabled so these tests do not touch rows of other tests.
    return Worker(registry=registry, reap_every_seconds=None)


def test_registered_handler_runs_once_and_job_becomes_done(job_kinds: list[str]) -> None:
    job_kinds.append("test.kind")
    registry = JobRegistry()
    calls: list[dict[str, str]] = []

    def handler(db: Session, payload: dict[str, str]) -> None:
        calls.append(payload)

    registry.register("test.kind", handler)
    job_id = _enqueue_committed("test.kind", {"resume_id": "r-1"})

    job = _run_until_attempted(_no_op_worker(registry), job_id)

    assert calls == [{"resume_id": "r-1"}]
    assert job.status is JobStatus.DONE
    assert job.attempts == 1
    assert job.locked_at is None


def test_handler_raising_retryable_error_requeues_job(job_kinds: list[str]) -> None:
    kind = _unique_kind(job_kinds)
    registry = JobRegistry()

    def handler(db: Session, payload: dict[str, str]) -> None:
        raise RetryableJobError("inference unavailable")

    registry.register(kind, handler)
    job_id = _enqueue_committed(kind)

    job = _run_until_attempted(_no_op_worker(registry), job_id)

    assert job.status is JobStatus.QUEUED
    assert job.attempts == 1
    assert job.last_error_code == "RetryableJobError"
    assert job.run_after > datetime.now(UTC)


def test_handler_raising_other_error_marks_failed_with_exception_name(
    job_kinds: list[str],
) -> None:
    kind = _unique_kind(job_kinds)
    registry = JobRegistry()

    def handler(db: Session, payload: dict[str, str]) -> None:
        raise KeyError("secret user text must not be stored")

    registry.register(kind, handler)
    job_id = _enqueue_committed(kind)

    job = _run_until_attempted(_no_op_worker(registry), job_id)

    assert job.status is JobStatus.FAILED
    assert job.last_error_code == "KeyError"


def test_handler_writes_are_rolled_back_when_it_fails(job_kinds: list[str]) -> None:
    kind = _unique_kind(job_kinds)
    marker_kind = _unique_kind(job_kinds, prefix="test.marker")
    registry = JobRegistry()

    def handler(db: Session, payload: dict[str, str]) -> None:
        enqueue(db, marker_kind, {"x": "y"}, run_after=datetime.now(UTC) + timedelta(days=1))
        raise RuntimeError("boom")

    registry.register(kind, handler)
    job_id = _enqueue_committed(kind)

    _run_until_attempted(_no_op_worker(registry), job_id)

    with Session(get_engine()) as session:
        leaked = session.execute(select(Job).where(Job.kind == marker_kind)).first()
    assert leaked is None


def test_job_without_registered_handler_is_marked_failed(job_kinds: list[str]) -> None:
    kind = _unique_kind(job_kinds)
    job_id = _enqueue_committed(kind)

    job = _run_until_attempted(_no_op_worker(JobRegistry()), job_id)

    assert job.status is JobStatus.FAILED
    assert job.last_error_code == "UnknownJobKind"


def test_periodic_with_zero_interval_runs_on_every_run_once(migrated_database: str) -> None:
    registry = JobRegistry()
    calls: list[Session] = []
    registry.register_periodic("test.periodic", 0, calls.append)
    worker = _no_op_worker(registry)

    worker.run_once()
    worker.run_once()

    assert len(calls) == 2


def test_periodic_with_interval_is_not_rerun_before_it_is_due(migrated_database: str) -> None:
    registry = JobRegistry()
    calls: list[Session] = []
    registry.register_periodic("test.hourly", 3600, calls.append)
    worker = _no_op_worker(registry)

    worker.run_once()
    worker.run_once()

    assert len(calls) == 1


def test_failing_periodic_does_not_stop_the_worker(job_kinds: list[str]) -> None:
    kind = _unique_kind(job_kinds)
    registry = JobRegistry()

    def broken(db: Session) -> None:
        raise RuntimeError("boom")

    handled: list[str] = []
    registry.register_periodic("test.broken", 0, broken)
    registry.register(kind, lambda db, payload: handled.append(payload["n"]))
    job_id = _enqueue_committed(kind)

    job = _run_until_attempted(_no_op_worker(registry), job_id)

    assert job.status is JobStatus.DONE
    assert handled == ["1"]


def test_register_validates_input() -> None:
    registry = JobRegistry()
    with pytest.raises(ValueError):
        registry.register("", lambda db, payload: None)
    with pytest.raises(ValueError):
        registry.register("x" * 65, lambda db, payload: None)
    with pytest.raises(ValueError):
        registry.register_periodic("p", -1, lambda db: None)


def test_register_rejects_a_second_handler_for_the_same_kind() -> None:
    registry = JobRegistry()
    registry.register("k", lambda db, payload: None)

    with pytest.raises(ValueError):
        registry.register("k", lambda db, payload: None)


def test_module_level_register_uses_default_registry() -> None:
    kind = f"test.module.{uuid.uuid4().hex[:8]}"
    name = f"test.module.periodic.{uuid.uuid4().hex[:8]}"

    def handler(db: Session, payload: dict[str, str]) -> None:
        return None

    def periodic(db: Session) -> None:
        return None

    register(kind, handler)
    register_periodic(name, 10, periodic)
    try:
        assert registry_module.default_registry.handler_for(kind) is handler
        assert name in {task.name for task in registry_module.default_registry.periodic_tasks()}
    finally:
        registry_module.default_registry.unregister(kind)
        registry_module.default_registry.unregister_periodic(name)


# --- Reaper (LAC-38) ---------------------------------------------------------------------


def _insert_running(
    db: Session, kind: str, locked_ago: timedelta, attempts: int, max_attempts: int = 5
) -> uuid.UUID:
    job_id = enqueue(db, kind, {"n": "1"})
    db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status=JobStatus.RUNNING,
            attempts=attempts,
            max_attempts=max_attempts,
            locked_at=datetime.now(UTC) - locked_ago,
        )
    )
    db.flush()
    return job_id


def _get(db: Session, job_id: uuid.UUID) -> Job:
    db.expire_all()
    return db.execute(select(Job).where(Job.id == job_id)).scalar_one()


def test_reaper_requeues_stale_running_job(db: Session) -> None:
    job_id = _insert_running(db, "test.reaper", timedelta(hours=2), attempts=1)

    reaped = reap_stale_jobs(db, timedelta(minutes=30))

    job = _get(db, job_id)
    assert reaped == 1
    assert job.status is JobStatus.QUEUED
    assert job.locked_at is None
    assert job.attempts == 1
    assert job.last_error_code == "StaleJobLock"


def test_reaper_fails_stale_job_that_exhausted_attempts(db: Session) -> None:
    job_id = _insert_running(db, "test.reaper", timedelta(hours=2), attempts=5, max_attempts=5)

    reap_stale_jobs(db, timedelta(minutes=30))

    job = _get(db, job_id)
    assert job.status is JobStatus.FAILED
    assert job.locked_at is None
    assert job.last_error_code == "StaleJobLock"


def test_reaper_keeps_recently_locked_running_job(db: Session) -> None:
    job_id = _insert_running(db, "test.reaper", timedelta(minutes=1), attempts=1)

    reaped = reap_stale_jobs(db, timedelta(minutes=30))

    assert reaped == 0
    assert _get(db, job_id).status is JobStatus.RUNNING


def test_worker_reaps_stale_jobs_on_first_run(job_kinds: list[str]) -> None:
    kind = _unique_kind(job_kinds)
    with Session(get_engine()) as session:
        stale_id = _insert_running(session, kind, timedelta(hours=2), attempts=1)
        session.commit()
    handled: list[str] = []
    registry = JobRegistry()
    registry.register(kind, lambda db, payload: handled.append(payload["n"]))
    worker = Worker(registry=registry, stale_after=timedelta(minutes=30), reap_every_seconds=60)

    for _ in range(20):
        worker.run_once()
        if handled:
            break

    job = _load(stale_id)
    assert handled == ["1"]
    assert job.status is JobStatus.DONE
    assert job.attempts == 2


def test_late_finish_after_reap_does_not_overwrite_new_owner(job_kinds: list[str]) -> None:
    """A worker whose job was reaped and re-claimed must not mark it done (fenced by attempts)."""
    kind = _unique_kind(job_kinds)
    registry = JobRegistry()

    def handler(db: Session, payload: dict[str, str]) -> None:
        # Simulate: while this handler ran, the job was reaped and claimed again elsewhere.
        with Session(get_engine()) as other:
            other.execute(
                update(Job)
                .where(Job.kind == kind)
                .values(attempts=Job.attempts + 1, status=JobStatus.RUNNING)
            )
            other.commit()

    registry.register(kind, handler)
    job_id = _enqueue_committed(kind)

    _no_op_worker(registry).run_once()

    job = _load(job_id)
    assert job.status is JobStatus.RUNNING
    assert job.attempts == 2


# --- Concurrency ---------------------------------------------------------------------------


def test_concurrent_workers_run_each_job_exactly_once(job_kinds: list[str]) -> None:
    kind = _unique_kind(job_kinds)
    job_ids = [_enqueue_committed(kind, {"n": str(n)}) for n in range(6)]
    lock = threading.Lock()
    calls: list[str] = []

    def handler(db: Session, payload: dict[str, str]) -> None:
        with lock:
            calls.append(payload["n"])

    registry = JobRegistry()
    registry.register(kind, handler)
    errors: list[BaseException] = []

    def run(worker: Worker) -> Callable[[], None]:
        def target() -> None:
            try:
                for _ in range(10):
                    worker.run_once()
            except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
                errors.append(exc)

        return target

    threads = [threading.Thread(target=run(_no_op_worker(registry))) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert sorted(calls) == [str(n) for n in range(6)]
    assert all(_load(job_id).status is JobStatus.DONE for job_id in job_ids)
