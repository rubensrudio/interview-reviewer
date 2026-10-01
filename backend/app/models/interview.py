"""Interview sessions, questions and chat messages (plan 7.5) and the `RequirementItem` schema.

A session keeps a frozen copy of the resume extraction (`snapshot`, CV-12), so later edits or
deletion of the resume never change it. At most one non-terminal session exists per user,
enforced by the partial unique index `ux_one_open_session_per_user` (PLAN-02).
"""

import enum
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SessionStatus(enum.StrEnum):
    COLLECTING_REQUIREMENTS = "collecting_requirements"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PREPARING_QUESTIONS = "preparing_questions"
    PREPARATION_FAILED = "preparation_failed"
    IN_INTERVIEW = "in_interview"
    EVALUATING = "evaluating"
    EVALUATION_FAILED = "evaluation_failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# A session in one of these states no longer blocks a new one (PLAN-02).
TERMINAL_STATUSES: frozenset[SessionStatus] = frozenset(
    {SessionStatus.COMPLETED, SessionStatus.CANCELLED, SessionStatus.EXPIRED}
)


class ExpectedLevel(enum.StrEnum):
    JUNIOR = "junior"
    MID_LEVEL = "mid-level"
    SENIOR = "senior"
    EXPERT = "expert"


class MessageRole(enum.StrEnum):
    CANDIDATE = "candidate"
    ASSISTANT = "assistant"


class MessageKind(enum.StrEnum):
    REQUIREMENTS = "requirements"
    REQUIREMENTS_REPLY = "requirements_reply"
    CLARIFICATION_REQUEST = "clarification_request"
    CLARIFICATION_REPLY = "clarification_reply"
    INFO = "info"


class RequirementItem(BaseModel):
    """One job requirement interpreted from the text the candidate pasted."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str
    original_terms: list[str]
    classification: Literal["required", "nice_to_have"]
    level: ExpectedLevel | None = None
    pending_clarification: bool = False
    clarification_question: str | None = None


def _pg_enum(enum_class: type[enum.Enum], name: str) -> Enum:
    return Enum(
        enum_class,
        name=name,
        values_callable=lambda members: [member.value for member in members],
    )


_TERMINAL_SQL = ", ".join(f"'{status.value}'" for status in sorted(TERMINAL_STATUSES))
OPEN_SESSION_PREDICATE = f"status NOT IN ({_TERMINAL_SQL})"


class InterviewSession(Base):
    __tablename__ = "interview_sessions"
    __table_args__ = (
        Index(
            "ux_one_open_session_per_user",
            "user_id",
            unique=True,
            postgresql_where=sql_text(OPEN_SESSION_PREDICATE),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    resume_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"), index=True
    )
    # PII (file names may contain a personal name).
    resume_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[SessionStatus] = mapped_column(
        _pg_enum(SessionStatus, "session_status"),
        default=SessionStatus.COLLECTING_REQUIREMENTS,
        server_default=SessionStatus.COLLECTING_REQUIREMENTS.value,
    )
    language: Mapped[str] = mapped_column(String(8), default="en", server_default="en")
    interview_level: Mapped[ExpectedLevel | None] = mapped_column(
        _pg_enum(ExpectedLevel, "expected_level")
    )
    # PII. Frozen list of `ExtractionItem.model_dump(mode="json")` (CV-12).
    snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    snapshot_minimal: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sql_text("false")
    )
    # PII.
    requirements_text: Mapped[str | None] = mapped_column(Text)
    # List of `RequirementItem.model_dump(mode="json")`.
    requirement_items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    non_technical: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    # `{planned_count, skills}`; fixed once the plan is confirmed (PLAN-10).
    proposal: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    planned_count: Mapped[int | None] = mapped_column(Integer)
    answered_count: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    preparation_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=sql_text("0")
    )
    evaluation_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=sql_text("0")
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Question(Base):
    __tablename__ = "questions"
    # The unique (session_id, position) index also serves lookups by session.
    __table_args__ = (UniqueConstraint("session_id", "position"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer)
    skill_name: Mapped[str] = mapped_column(String(255))
    expected_level: Mapped[ExpectedLevel | None] = mapped_column(
        _pg_enum(ExpectedLevel, "expected_level")
    )
    text: Mapped[str] = mapped_column(Text)
    # Never serialized outside the final report (INTV-14).
    reference_points: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    # Frozen copies: list of `SourceRef.model_dump(mode="json")`.
    sources: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    no_verified_source: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sql_text("false")
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE")
    )
    role: Mapped[MessageRole] = mapped_column(_pg_enum(MessageRole, "message_role"))
    kind: Mapped[MessageKind] = mapped_column(_pg_enum(MessageKind, "message_kind"))
    question_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("questions.id", ondelete="SET NULL"), index=True
    )
    # PII.
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index("ix_messages_session_id_created_at", Message.session_id, Message.created_at)
