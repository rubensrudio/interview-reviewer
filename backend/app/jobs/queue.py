"""Postgres-backed job queue (DA-4): ``SELECT ... FOR UPDATE SKIP LOCKED`` on ``jobs``.

None of these functions commit. The caller owns the transaction (CT-2): commit after
``enqueue`` together with the business write, and commit right after ``claim_next`` so the
``running`` status becomes visible and the row lock is released before the work starts.
Time comparisons use ``clock_timestamp()`` (the database clock), not the transaction start.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from app.models.job import Job, JobStatus
from app.observability import log_event

KIND_MAX_LENGTH = 64
ERROR_CODE_MAX_LENGTH = 64


def _validate_short_text(name: str, value: str, max_length: int) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value or len(value) > max_length:
        raise ValueError(f"{name} must have between 1 and {max_length} characters")


def _validate_payload(payload: dict[str, str]) -> None:
    if not isinstance(payload, dict):
        raise TypeError("job payload must be a dict")
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            # Payload holds identifiers only (DA-4); values are never echoed back.
            raise TypeError("job payload keys and values must be strings")


def enqueue(
    db: Session, kind: str, payload: dict[str, str], run_after: datetime | None = None
) -> uuid.UUID:
    """Add a queued job and flush it; returns its id. The caller commits."""
    _validate_short_text("job kind", kind, KIND_MAX_LENGTH)
    _validate_payload(payload)
    if run_after is not None and run_after.tzinfo is None:
        raise ValueError("run_after must be timezone-aware")

    job = Job(kind=kind, payload=dict(payload), status=JobStatus.QUEUED)
    if run_after is not None:
        job.run_after = run_after
    db.add(job)
    db.flush()
    log_event("job.enqueued", job_id=str(job.id), kind=kind)
    return job.id


def claim_next(db: Session) -> Job | None:
    """Lock the oldest due queued job, mark it running and count the attempt.

    Concurrent callers on other connections skip the locked row, so a job is never
    claimed twice.
    """
    statement = (
        select(Job)
        .where(Job.status == JobStatus.QUEUED, Job.run_after <= func.clock_timestamp())
        .order_by(Job.run_after, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = db.execute(statement).scalar_one_or_none()
    if job is None:
        return None

    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.locked_at = func.clock_timestamp()
    db.flush()
    db.refresh(job)
    log_event("job.claimed", job_id=str(job.id), kind=job.kind, attempts=job.attempts)
    return job


def renew_lock(db: Session, job_id: uuid.UUID, attempts: int) -> bool:
    """Refresh ``locked_at`` of a running job only if ``attempts`` still matches (fencing).

    Returns False when the job is no longer this execution's (reaped, claimed again or
    finished). The caller commits, in a session of its own (never the handler's).
    """
    statement = (
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == JobStatus.RUNNING,
            Job.attempts == attempts,
        )
        .values(locked_at=func.clock_timestamp())
        .execution_options(synchronize_session=False)
    )
    result = cast(CursorResult[Any], db.execute(statement))
    return result.rowcount == 1


def mark_done(db: Session, job: Job) -> None:
    """Mark a claimed job as done. The caller commits."""
    job.status = JobStatus.DONE
    job.locked_at = None
    db.flush()
    log_event("job.done", job_id=str(job.id), kind=job.kind, attempts=job.attempts)


def mark_failed(db: Session, job: Job, error_code: str, retry: bool) -> None:
    """Record a failure; retry with ``2^attempts`` seconds of backoff up to ``max_attempts``.

    ``error_code`` must be a short code (e.g. the exception class name), never a message
    that could carry user data.
    """
    _validate_short_text("error_code", error_code, ERROR_CODE_MAX_LENGTH)
    job.last_error_code = error_code
    job.locked_at = None

    if retry and job.attempts < job.max_attempts:
        delay = timedelta(seconds=2**job.attempts)
        job.status = JobStatus.QUEUED
        job.run_after = func.clock_timestamp() + delay
        event = "job.retry_scheduled"
    else:
        job.status = JobStatus.FAILED
        event = "job.failed"

    db.flush()
    db.refresh(job)
    log_event(
        event,
        job_id=str(job.id),
        kind=job.kind,
        attempts=job.attempts,
        error_code=error_code,
    )
