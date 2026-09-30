"""Integration tests for the Postgres job queue (CT-6, DA-4)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.db import get_engine
from app.jobs.queue import claim_next, enqueue, mark_done, mark_failed
from app.models.job import Job, JobStatus


def _make_due(db: Session, job_id: uuid.UUID) -> None:
    """Move a rescheduled job back to the past so the next claim can pick it up."""
    db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(run_after=datetime.now(UTC) - timedelta(minutes=1))
    )
    db.flush()
    db.expire_all()


def test_enqueue_persists_queued_job_and_returns_id(db: Session) -> None:
    resume_id = str(uuid.uuid4())

    job_id = enqueue(db, "resume.process", {"resume_id": resume_id})

    job = db.execute(select(Job).where(Job.id == job_id)).scalar_one()
    assert isinstance(job_id, uuid.UUID)
    assert job.kind == "resume.process"
    assert job.payload == {"resume_id": resume_id}
    assert job.status is JobStatus.QUEUED
    assert job.attempts == 0


def test_enqueue_rejects_non_string_payload_value(db: Session) -> None:
    with pytest.raises(TypeError):
        enqueue(db, "k", {"a": 1})  # type: ignore[dict-item]


def test_enqueue_rejects_non_string_payload_key(db: Session) -> None:
    with pytest.raises(TypeError):
        enqueue(db, "k", {1: "a"})  # type: ignore[dict-item]


def test_enqueue_rejects_non_dict_payload(db: Session) -> None:
    with pytest.raises(TypeError):
        enqueue(db, "k", ["a"])  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["", "x" * 65])
def test_enqueue_rejects_invalid_kind(db: Session, kind: str) -> None:
    with pytest.raises(ValueError):
        enqueue(db, kind, {"a": "b"})


def test_claim_next_marks_job_running(db: Session) -> None:
    job_id = enqueue(db, "k", {"a": "b"})

    job = claim_next(db)

    assert job is not None
    assert job.id == job_id
    assert job.status is JobStatus.RUNNING
    assert job.attempts == 1
    assert job.locked_at is not None


def test_claim_next_returns_none_when_queue_empty(db: Session) -> None:
    assert claim_next(db) is None


def test_claim_next_skips_job_scheduled_in_future(db: Session) -> None:
    enqueue(db, "k", {"a": "b"}, run_after=datetime.now(UTC) + timedelta(hours=1))

    assert claim_next(db) is None


def test_claim_next_returns_oldest_due_job_first(db: Session) -> None:
    now = datetime.now(UTC)
    later = enqueue(db, "k", {"n": "2"}, run_after=now - timedelta(minutes=1))
    earlier = enqueue(db, "k", {"n": "1"}, run_after=now - timedelta(minutes=5))

    first = claim_next(db)
    second = claim_next(db)

    assert first is not None and first.id == earlier
    assert second is not None and second.id == later


def test_mark_done_sets_status_done(db: Session) -> None:
    enqueue(db, "k", {"a": "b"})
    job = claim_next(db)
    assert job is not None

    mark_done(db, job)

    db.expire_all()
    stored = db.get(Job, job.id)
    assert stored is not None
    assert stored.status is JobStatus.DONE
    assert stored.locked_at is None


def test_mark_failed_with_retry_reschedules_with_backoff(db: Session) -> None:
    job_id = enqueue(db, "k", {"a": "b"})
    job = claim_next(db)
    assert job is not None
    before = datetime.now(UTC)

    mark_failed(db, job, "TimeoutError", retry=True)

    db.expire_all()
    stored = db.get(Job, job_id)
    assert stored is not None
    assert stored.status is JobStatus.QUEUED
    assert stored.last_error_code == "TimeoutError"
    assert stored.locked_at is None
    # attempts == 1 -> 2^1 seconds of backoff, measured from the database clock.
    assert stored.run_after > before
    assert stored.run_after - before < timedelta(seconds=30)
    assert claim_next(db) is None


def test_mark_failed_without_retry_sets_failed(db: Session) -> None:
    job_id = enqueue(db, "k", {"a": "b"})
    job = claim_next(db)
    assert job is not None

    mark_failed(db, job, "ValueError", retry=False)

    db.expire_all()
    stored = db.get(Job, job_id)
    assert stored is not None
    assert stored.status is JobStatus.FAILED
    assert stored.last_error_code == "ValueError"


def test_mark_failed_after_max_attempts_sets_failed(db: Session) -> None:
    job_id = enqueue(db, "k", {"a": "b"})
    max_attempts = db.get(Job, job_id).max_attempts  # type: ignore[union-attr]

    for _ in range(max_attempts):
        job = claim_next(db)
        assert job is not None and job.id == job_id
        mark_failed(db, job, "ConnectionError", retry=True)
        _make_due(db, job_id)

    stored = db.get(Job, job_id)
    assert stored is not None
    assert stored.status is JobStatus.FAILED
    assert stored.attempts == max_attempts
    assert claim_next(db) is None


def test_mark_failed_rejects_invalid_error_code(db: Session) -> None:
    enqueue(db, "k", {"a": "b"})
    job = claim_next(db)
    assert job is not None

    with pytest.raises(ValueError):
        mark_failed(db, job, "", retry=True)


@pytest.fixture
def committed_jobs(migrated_database: str) -> Iterator[list[uuid.UUID]]:
    """Two due jobs committed for real, so separate connections can see and lock them."""
    kind = f"test.concurrent.{uuid.uuid4().hex[:8]}"
    with Session(get_engine()) as session:
        ids = [enqueue(session, kind, {"n": str(n)}) for n in range(2)]
        session.commit()
    try:
        yield ids
    finally:
        with Session(get_engine()) as session:
            session.execute(delete(Job).where(Job.kind == kind))
            session.commit()


def test_concurrent_claims_never_return_same_job(committed_jobs: list[uuid.UUID]) -> None:
    engine = get_engine()
    with Session(engine) as first, Session(engine) as second:
        claimed_first = claim_next(first)
        claimed_second = claim_next(second)
        assert claimed_first is not None and claimed_second is not None
        first_id, second_id = claimed_first.id, claimed_second.id
        # Both transactions are still open, so both row locks are held here.
        third = Session(engine)
        try:
            claimed_third = claim_next(third)
        finally:
            third.rollback()
            third.close()
        first.rollback()
        second.rollback()

    assert first_id != second_id
    assert {first_id, second_id} == set(committed_jobs)
    assert claimed_third is None
