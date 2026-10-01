"""Tests for the interview session tables (migration 0005_interviews) and their models."""

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

from app.models import ExpectedLevel as ExportedExpectedLevel
from app.models import InterviewSession as ExportedInterviewSession
from app.models import Message as ExportedMessage
from app.models import MessageKind as ExportedMessageKind
from app.models import Question as ExportedQuestion
from app.models import RequirementItem as ExportedRequirementItem
from app.models import SessionStatus as ExportedSessionStatus
from app.models import User
from app.models.interview import (
    TERMINAL_STATUSES,
    ExpectedLevel,
    InterviewSession,
    Message,
    MessageKind,
    MessageRole,
    Question,
    RequirementItem,
    SessionStatus,
)
from app.models.knowledge import SourceRef
from app.models.resume import ExtractionItem, Resume, ResumeStatus

COLLECTED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

SNAPSHOT_ITEM: dict[str, Any] = {
    "id": "item-1",
    "kind": "skill",
    "fields": {"name": "Python"},
    "origin": "explicit",
    "evidence": ["5 years of Python"],
}


def _user(db: Session) -> User:
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    return user


def _resume(db: Session, user: User) -> Resume:
    resume = Resume(
        user_id=user.id, filename="cv.pdf", status=ResumeStatus.READY, extraction=[SNAPSHOT_ITEM]
    )
    db.add(resume)
    db.flush()
    return resume


def _session(db: Session, user: User, **overrides: Any) -> InterviewSession:
    data: dict[str, Any] = {"user_id": user.id, "snapshot": [SNAPSHOT_ITEM]}
    data.update(overrides)
    session = InterviewSession(**data)
    db.add(session)
    db.flush()
    return session


def _requirement(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "req-1",
        "name": "Python",
        "original_terms": ["Python 3"],
        "classification": "required",
        "level": "senior",
        "pending_clarification": False,
        "clarification_question": None,
    }
    data.update(overrides)
    return data


# --- enums and pydantic schema (no database) ---


def test_models_package_exports_interview_models() -> None:
    assert ExportedInterviewSession is InterviewSession
    assert ExportedSessionStatus is SessionStatus
    assert ExportedQuestion is Question
    assert ExportedMessage is Message
    assert ExportedMessageKind is MessageKind
    assert ExportedRequirementItem is RequirementItem
    assert ExportedExpectedLevel is ExpectedLevel


def test_session_status_values() -> None:
    assert [status.value for status in SessionStatus] == [
        "collecting_requirements",
        "awaiting_confirmation",
        "preparing_questions",
        "preparation_failed",
        "in_interview",
        "evaluating",
        "evaluation_failed",
        "completed",
        "cancelled",
        "expired",
    ]
    assert {SessionStatus.COMPLETED, SessionStatus.CANCELLED, SessionStatus.EXPIRED} == set(
        TERMINAL_STATUSES
    )


def test_expected_level_values() -> None:
    assert [level.value for level in ExpectedLevel] == ["junior", "mid-level", "senior", "expert"]


def test_message_enums_values() -> None:
    assert [role.value for role in MessageRole] == ["candidate", "assistant"]
    assert [kind.value for kind in MessageKind] == [
        "requirements",
        "requirements_reply",
        "clarification_request",
        "clarification_reply",
        "info",
    ]


def test_requirement_item_json_roundtrip() -> None:
    item = RequirementItem.model_validate(_requirement())
    assert item.level is ExpectedLevel.SENIOR
    assert RequirementItem.model_validate(item.model_dump(mode="json")) == item
    assert item.model_dump(mode="json")["level"] == "senior"


def test_requirement_item_level_is_optional() -> None:
    item = RequirementItem.model_validate(_requirement(level=None, classification="nice_to_have"))
    assert item.level is None
    assert item.classification == "nice_to_have"


@pytest.mark.parametrize(
    "overrides",
    [
        {"classification": "optional"},
        {"level": "principal"},
        {"id": ""},
        {"extra": "field"},
    ],
)
def test_requirement_item_rejects_invalid_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RequirementItem.model_validate(_requirement(**overrides))


# --- interview_sessions table ---


def test_plan_01_new_session_defaults(db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user)
    session = _session(db, user, resume_id=resume.id, resume_name="cv.pdf")
    db.commit()
    session_id = session.id
    db.expunge_all()

    loaded = db.get(InterviewSession, session_id)
    assert loaded is not None
    assert isinstance(loaded.id, uuid.UUID)
    assert loaded.status is SessionStatus.COLLECTING_REQUIREMENTS
    assert loaded.language == "en"
    assert loaded.interview_level is None
    assert loaded.snapshot_minimal is False
    assert loaded.requirements_text is None
    assert loaded.requirement_items == []
    assert loaded.non_technical == []
    assert loaded.proposal is None
    assert loaded.planned_count is None
    assert loaded.answered_count == 0
    assert loaded.preparation_attempts == 0
    assert loaded.evaluation_attempts == 0
    assert loaded.completed_at is None
    assert loaded.created_at.tzinfo is not None
    assert abs(datetime.now(UTC) - loaded.created_at) < timedelta(minutes=5)
    assert loaded.last_activity_at.tzinfo is not None


def test_cv_12_snapshot_is_independent_from_later_resume_changes(db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user)
    session = _session(db, user, resume_id=resume.id, snapshot=list(resume.extraction or []))
    resume.extraction = [dict(SNAPSHOT_ITEM, fields={"name": "Go"})]
    db.commit()
    session_id = session.id
    db.expunge_all()

    loaded = db.get(InterviewSession, session_id)
    assert loaded is not None
    assert [ExtractionItem.model_validate(raw) for raw in loaded.snapshot] == [
        ExtractionItem.model_validate(SNAPSHOT_ITEM)
    ]


def test_plan_10_plan_fields_are_persisted(db: Session) -> None:
    user = _user(db)
    session = _session(db, user, interview_level=ExpectedLevel.MID_LEVEL)
    session.requirement_items = [RequirementItem.model_validate(_requirement()).model_dump()]
    session.non_technical = ["Good communication"]
    session.proposal = {"planned_count": 5, "skills": ["Python"]}
    session.planned_count = 5
    session.status = SessionStatus.PREPARING_QUESTIONS
    db.commit()
    session_id = session.id
    db.expunge_all()

    loaded = db.get(InterviewSession, session_id)
    assert loaded is not None
    assert loaded.interview_level is ExpectedLevel.MID_LEVEL
    assert [RequirementItem.model_validate(raw) for raw in loaded.requirement_items] == [
        RequirementItem.model_validate(_requirement())
    ]
    assert loaded.non_technical == ["Good communication"]
    assert loaded.proposal == {"planned_count": 5, "skills": ["Python"]}
    assert loaded.planned_count == 5
    assert loaded.status is SessionStatus.PREPARING_QUESTIONS


def test_plan_02_second_open_session_for_same_user_is_rejected(db: Session) -> None:
    user = _user(db)
    _session(db, user)
    with pytest.raises(IntegrityError):
        _session(db, user)


def test_plan_02_second_session_accepted_after_first_is_cancelled(db: Session) -> None:
    user = _user(db)
    first = _session(db, user)
    first.status = SessionStatus.CANCELLED
    db.flush()

    second = _session(db, user)
    assert second.status is SessionStatus.COLLECTING_REQUIREMENTS


@pytest.mark.parametrize("terminal", [SessionStatus.COMPLETED, SessionStatus.EXPIRED])
def test_plan_02_other_terminal_statuses_free_the_slot(
    db: Session, terminal: SessionStatus
) -> None:
    user = _user(db)
    _session(db, user, status=terminal)
    _session(db, user, status=terminal)
    _session(db, user)


@pytest.mark.parametrize(
    "open_status",
    [s for s in SessionStatus if s not in TERMINAL_STATUSES],
)
def test_plan_02_every_non_terminal_status_holds_the_slot(
    db: Session, open_status: SessionStatus
) -> None:
    user = _user(db)
    _session(db, user, status=open_status)
    with pytest.raises(IntegrityError):
        _session(db, user)


def test_plan_02_open_sessions_of_different_users_coexist(db: Session) -> None:
    _session(db, _user(db))
    _session(db, _user(db))


def test_one_open_session_index_is_partial_and_unique(db: Session) -> None:
    definition = db.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ux_one_open_session_per_user'")
    ).scalar_one()
    assert definition.startswith("CREATE UNIQUE INDEX")
    assert "(user_id)" in definition
    for status in ("completed", "cancelled", "expired"):
        assert status in definition


def test_deleting_resume_sets_session_resume_id_to_null(db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user)
    session = _session(db, user, resume_id=resume.id, resume_name="cv.pdf")
    session_id = session.id

    db.delete(resume)
    db.flush()
    db.expunge_all()

    loaded = db.get(InterviewSession, session_id)
    assert loaded is not None
    assert loaded.resume_id is None
    assert loaded.snapshot == [SNAPSHOT_ITEM]


def test_deleting_user_cascades_sessions_questions_and_messages(db: Session) -> None:
    user = _user(db)
    session = _session(db, user)
    question = Question(session_id=session.id, position=1, skill_name="Python", text="Why?")
    db.add(question)
    db.flush()
    db.add(
        Message(
            session_id=session.id,
            role=MessageRole.ASSISTANT,
            kind=MessageKind.INFO,
            question_id=question.id,
            content="Hello",
        )
    )
    db.flush()
    session_id = session.id

    db.delete(user)
    db.flush()
    db.expunge_all()

    for model in (InterviewSession, Question, Message):
        column = model.id if model is InterviewSession else model.session_id
        count = db.scalar(select(func.count()).select_from(model).where(column == session_id))
        assert count == 0, model.__tablename__


def test_session_requires_existing_user(db: Session) -> None:
    db.add(InterviewSession(user_id=uuid.uuid4(), snapshot=[]))
    with pytest.raises(IntegrityError):
        db.flush()


def test_session_enums_are_postgres_enums(db: Session) -> None:
    def labels(name: str) -> list[str]:
        rows = db.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = :name ORDER BY e.enumsortorder"
            ),
            {"name": name},
        ).scalars()
        return list(rows)

    assert labels("session_status") == [s.value for s in SessionStatus]
    assert labels("expected_level") == [level.value for level in ExpectedLevel]
    assert labels("message_role") == [role.value for role in MessageRole]
    assert labels("message_kind") == [kind.value for kind in MessageKind]


# --- questions table ---


def test_question_persists_frozen_sources_and_reference_points(db: Session) -> None:
    user = _user(db)
    session = _session(db, user)
    source = SourceRef(
        url="https://docs.python.org/3/", title="Python", collected_at=COLLECTED_AT, excerpt="x"
    )
    question = Question(
        session_id=session.id,
        position=1,
        skill_name="Python",
        expected_level=ExpectedLevel.SENIOR,
        text="Explain the GIL.",
        reference_points=["Global lock", "Threads"],
        sources=[source.model_dump(mode="json")],
    )
    db.add(question)
    db.commit()
    question_id = question.id
    db.expunge_all()

    loaded = db.get(Question, question_id)
    assert loaded is not None
    assert loaded.expected_level is ExpectedLevel.SENIOR
    assert loaded.reference_points == ["Global lock", "Threads"]
    assert [SourceRef.model_validate(raw) for raw in loaded.sources] == [source]
    assert loaded.no_verified_source is False


def test_question_defaults(db: Session) -> None:
    user = _user(db)
    session = _session(db, user)
    question = Question(session_id=session.id, position=1, skill_name="SQL", text="Joins?")
    db.add(question)
    db.flush()
    db.expire(question)
    assert question.expected_level is None
    assert question.reference_points == []
    assert question.sources == []
    assert question.no_verified_source is False


def test_question_position_is_unique_per_session(db: Session) -> None:
    user = _user(db)
    session = _session(db, user)
    db.add(Question(session_id=session.id, position=1, skill_name="A", text="Q1"))
    db.flush()
    db.add(Question(session_id=session.id, position=1, skill_name="B", text="Q2"))
    with pytest.raises(IntegrityError):
        db.flush()


# --- messages table ---


def test_message_is_persisted(db: Session) -> None:
    user = _user(db)
    session = _session(db, user)
    message = Message(
        session_id=session.id,
        role=MessageRole.CANDIDATE,
        kind=MessageKind.REQUIREMENTS,
        content="We need Python and SQL.",
    )
    db.add(message)
    db.commit()
    message_id = message.id
    db.expunge_all()

    loaded = db.get(Message, message_id)
    assert loaded is not None
    assert loaded.role is MessageRole.CANDIDATE
    assert loaded.kind is MessageKind.REQUIREMENTS
    assert loaded.question_id is None
    assert loaded.content == "We need Python and SQL."
    assert loaded.created_at.tzinfo is not None


def test_deleting_session_cascades_questions_and_messages(db: Session) -> None:
    user = _user(db)
    session = _session(db, user)
    db.add(Question(session_id=session.id, position=1, skill_name="A", text="Q1"))
    db.add(
        Message(
            session_id=session.id, role=MessageRole.ASSISTANT, kind=MessageKind.INFO, content="Hi"
        )
    )
    db.flush()
    session_id = session.id

    db.delete(session)
    db.flush()
    db.expunge_all()

    assert db.scalar(select(func.count()).where(Question.session_id == session_id)) == 0
    assert db.scalar(select(func.count()).where(Message.session_id == session_id)) == 0


def test_interview_tables_exist(db: Session) -> None:
    tables = set(inspect(db.connection()).get_table_names())
    assert {"interview_sessions", "questions", "messages"} <= tables


# --- migration chain ---


def test_migration_0005_downgrade_and_upgrade(
    migrated_database: str, alembic_config: Config
) -> None:
    head = ScriptDirectory.from_config(alembic_config).get_current_head()
    engine = create_engine(migrated_database)
    try:
        downgrade(alembic_config, "0004")
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == "0004"
            tables = set(inspect(connection).get_table_names())
            assert not tables & {"interview_sessions", "questions", "messages"}
            enum_count = connection.execute(
                text(
                    "SELECT count(*) FROM pg_type WHERE typname IN "
                    "('session_status', 'expected_level', 'message_role', 'message_kind')"
                )
            ).scalar_one()
            assert enum_count == 0
    finally:
        upgrade(alembic_config, "head")

    try:
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == head
            tables = set(inspect(connection).get_table_names())
            assert {"interview_sessions", "questions", "messages"} <= tables
    finally:
        engine.dispose()
