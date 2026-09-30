"""Integration tests for the `jobs` table (migration 0001_jobs) and the `Job` model."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.models import Job as ExportedJob
from app.models.job import Job, JobStatus


def test_job_status_values() -> None:
    assert [status.value for status in JobStatus] == ["queued", "running", "done", "failed"]


def test_models_package_exports_job() -> None:
    assert ExportedJob is Job


def test_insert_and_read_job_with_identifier_payload(db: Session) -> None:
    resume_id = str(uuid.uuid4())
    job = Job(kind="resume.process", payload={"resume_id": resume_id})
    db.add(job)
    db.commit()
    job_id = job.id
    db.expunge_all()

    loaded = db.execute(select(Job).where(Job.id == job_id)).scalar_one()
    assert isinstance(loaded.id, uuid.UUID)
    assert loaded.kind == "resume.process"
    assert loaded.payload == {"resume_id": resume_id}
    assert loaded.status is JobStatus.QUEUED
    assert loaded.attempts == 0
    assert loaded.max_attempts == 5
    assert loaded.locked_at is None
    assert loaded.last_error_code is None
    now = datetime.now(UTC)
    for moment in (loaded.run_after, loaded.created_at, loaded.updated_at):
        assert moment.tzinfo is not None
        assert abs(now - moment) < timedelta(minutes=5)


def test_status_is_stored_as_postgres_enum(db: Session) -> None:
    labels = db.execute(
        text(
            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'job_status' ORDER BY e.enumsortorder"
        )
    ).scalars()
    assert list(labels) == ["queued", "running", "done", "failed"]


def test_status_rejects_unknown_value(db: Session) -> None:
    with pytest.raises(DataError):
        db.execute(
            text(
                "INSERT INTO jobs (id, kind, payload, status) "
                "VALUES (gen_random_uuid(), 'resume.process', '{}'::jsonb, 'unknown')"
            )
        )


def test_payload_is_required(db: Session) -> None:
    db.add(Job(kind="resume.process", payload=None))
    with pytest.raises(IntegrityError):
        db.flush()


def test_jobs_table_has_status_run_after_index(db: Session) -> None:
    indexes = inspect(db.connection()).get_indexes("jobs")
    assert any(index["column_names"] == ["status", "run_after"] for index in indexes)


def test_payload_column_is_jsonb(db: Session) -> None:
    data_type = db.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'jobs' AND column_name = 'payload'"
        )
    ).scalar_one()
    assert data_type == "jsonb"
