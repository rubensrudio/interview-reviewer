"""Tests for the resume table (migration 0003_resumes), its model and `ExtractionItem`."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ExtractionItem as ExportedExtractionItem
from app.models import Resume as ExportedResume
from app.models import ResumeStatus as ExportedResumeStatus
from app.models import User
from app.models.resume import ExtractionItem, Resume, ResumeStatus


def _item(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "item-1",
        "kind": "skill",
        "fields": {"name": "Python"},
        "origin": "explicit",
        "evidence": ["5 years of Python"],
    }
    data.update(overrides)
    return data


def _user(db: Session) -> User:
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    return user


# --- ExtractionItem (pure pydantic, no database) ---


def test_resume_status_values() -> None:
    assert [status.value for status in ResumeStatus] == [
        "received",
        "processing",
        "ready",
        "failed",
    ]


def test_models_package_exports_resume_models() -> None:
    assert ExportedResume is Resume
    assert ExportedResumeStatus is ResumeStatus
    assert ExportedExtractionItem is ExtractionItem


@pytest.mark.parametrize("origin", ["explicit", "inferred", "user_provided"])
def test_extraction_item_accepts_known_origins(origin: str) -> None:
    item = ExtractionItem.model_validate(_item(origin=origin))
    assert item.origin == origin


@pytest.mark.parametrize("origin", ["llm", "EXPLICIT", "", "user"])
def test_cv_10_extraction_item_rejects_unknown_origin(origin: str) -> None:
    with pytest.raises(ValidationError):
        ExtractionItem.model_validate(_item(origin=origin))


@pytest.mark.parametrize("origin", ["explicit", "inferred"])
def test_cv_10_extraction_item_without_evidence_requires_user_provided(origin: str) -> None:
    with pytest.raises(ValidationError):
        ExtractionItem.model_validate(_item(origin=origin, evidence=[]))


def test_cv_10_user_provided_item_without_evidence_is_valid() -> None:
    item = ExtractionItem.model_validate(_item(origin="user_provided", evidence=[]))
    assert item.evidence == []


@pytest.mark.parametrize("kind", ["experience", "education", "skill"])
def test_extraction_item_accepts_known_kinds(kind: str) -> None:
    assert ExtractionItem.model_validate(_item(kind=kind)).kind == kind


def test_extraction_item_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        ExtractionItem.model_validate(_item(kind="certification"))


def test_extraction_item_rejects_non_string_field_values() -> None:
    with pytest.raises(ValidationError):
        ExtractionItem.model_validate(_item(fields={"name": ["Python"]}))


def test_extraction_item_json_roundtrip() -> None:
    item = ExtractionItem.model_validate(
        _item(
            kind="experience",
            fields={"title": "Engineer", "organization": "ACME", "start": "2020"},
            origin="inferred",
        )
    )
    assert ExtractionItem.model_validate(item.model_dump(mode="json")) == item


# --- resumes table ---


def test_cv_01_insert_resume_defaults_to_received(db: Session) -> None:
    user = _user(db)
    resume = Resume(user_id=user.id, filename="cv.pdf")
    db.add(resume)
    db.commit()
    resume_id = resume.id
    db.expunge_all()

    loaded = db.get(Resume, resume_id)
    assert loaded is not None
    assert isinstance(loaded.id, uuid.UUID)
    assert loaded.filename == "cv.pdf"
    assert loaded.status is ResumeStatus.RECEIVED
    assert loaded.failure_code is None
    assert loaded.storage_key is None
    assert loaded.extracted_text is None
    assert loaded.extraction is None
    assert loaded.uploaded_at.tzinfo is not None
    assert abs(datetime.now(UTC) - loaded.uploaded_at) < timedelta(minutes=5)
    assert loaded.updated_at.tzinfo is not None


def test_cv_05_status_transitions_are_persisted(db: Session) -> None:
    user = _user(db)
    resume = Resume(user_id=user.id, filename="cv.pdf")
    db.add(resume)
    db.flush()
    for status in (ResumeStatus.PROCESSING, ResumeStatus.FAILED):
        resume.status = status
        db.flush()
        db.expire(resume)
        assert resume.status is status
    resume.failure_code = "NOT_ENGLISH"
    db.flush()
    db.expire(resume)
    assert resume.failure_code == "NOT_ENGLISH"


def test_cv_10_extraction_is_stored_as_jsonb(db: Session) -> None:
    user = _user(db)
    items = [
        ExtractionItem.model_validate(_item()),
        ExtractionItem.model_validate(
            _item(id="item-2", fields={"name": "SQL"}, origin="user_provided", evidence=[])
        ),
    ]
    resume = Resume(
        user_id=user.id,
        filename="cv.pdf",
        status=ResumeStatus.READY,
        extraction=[item.model_dump(mode="json") for item in items],
    )
    db.add(resume)
    db.commit()
    resume_id = resume.id
    db.expunge_all()

    loaded = db.get(Resume, resume_id)
    assert loaded is not None
    assert loaded.extraction is not None
    assert [ExtractionItem.model_validate(raw) for raw in loaded.extraction] == items
    column_type = db.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'resumes' AND column_name = 'extraction'"
        )
    ).scalar_one()
    assert column_type == "jsonb"


def test_resume_requires_existing_user(db: Session) -> None:
    db.add(Resume(user_id=uuid.uuid4(), filename="cv.pdf"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_deleting_user_cascades_resumes(db: Session) -> None:
    user = _user(db)
    db.add_all([Resume(user_id=user.id, filename=f"cv{i}.pdf") for i in range(2)])
    db.flush()
    user_id = user.id

    db.delete(user)
    db.flush()
    db.expunge_all()

    count = db.scalar(select(func.count()).select_from(Resume).where(Resume.user_id == user_id))
    assert count == 0


def test_resume_status_is_stored_as_postgres_enum(db: Session) -> None:
    labels = db.execute(
        text(
            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'resume_status' ORDER BY e.enumsortorder"
        )
    ).scalars()
    assert list(labels) == ["received", "processing", "ready", "failed"]


def test_resumes_index_on_user_and_upload_date_desc(db: Session) -> None:
    definition = db.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_resumes_user_id_uploaded_at'")
    ).scalar_one()
    assert "(user_id, uploaded_at DESC)" in definition


def test_no_separate_extraction_items_table(db: Session) -> None:
    tables = set(inspect(db.connection()).get_table_names())
    assert "resumes" in tables
    assert not tables & {"extraction_items", "resume_items"}


# --- migration chain ---


def test_migration_0003_downgrade_and_upgrade(
    migrated_database: str, alembic_config: Config
) -> None:
    head = ScriptDirectory.from_config(alembic_config).get_current_head()
    engine = create_engine(migrated_database)
    try:
        downgrade(alembic_config, "0002")
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == "0002"
            assert "resumes" not in inspect(connection).get_table_names()
            enum_count = connection.execute(
                text("SELECT count(*) FROM pg_type WHERE typname = 'resume_status'")
            ).scalar_one()
            assert enum_count == 0
    finally:
        upgrade(alembic_config, "head")

    try:
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == head
            assert "resumes" in inspect(connection).get_table_names()
    finally:
        engine.dispose()
