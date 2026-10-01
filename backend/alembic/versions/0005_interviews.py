"""interviews

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-30 00:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

session_status = postgresql.ENUM(
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
    name="session_status",
    create_type=False,
)
expected_level = postgresql.ENUM(
    "junior", "mid-level", "senior", "expert", name="expected_level", create_type=False
)
message_role = postgresql.ENUM("candidate", "assistant", name="message_role", create_type=False)
message_kind = postgresql.ENUM(
    "requirements",
    "requirements_reply",
    "clarification_request",
    "clarification_reply",
    "info",
    name="message_kind",
    create_type=False,
)

ENUMS = (session_status, expected_level, message_role, message_kind)
EMPTY_JSON_ARRAY = sa.text("'[]'::jsonb")


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    for enum_type in ENUMS:
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "interview_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("resume_id", sa.Uuid(), nullable=True),
        sa.Column("resume_name", sa.String(length=255), nullable=True),
        sa.Column(
            "status", session_status, server_default="collecting_requirements", nullable=False
        ),
        sa.Column("language", sa.String(length=8), server_default="en", nullable=False),
        sa.Column("interview_level", expected_level, nullable=True),
        sa.Column("snapshot", postgresql.JSONB(), server_default=EMPTY_JSON_ARRAY, nullable=False),
        sa.Column("snapshot_minimal", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("requirements_text", sa.Text(), nullable=True),
        sa.Column(
            "requirement_items", postgresql.JSONB(), server_default=EMPTY_JSON_ARRAY, nullable=False
        ),
        sa.Column(
            "non_technical", postgresql.JSONB(), server_default=EMPTY_JSON_ARRAY, nullable=False
        ),
        sa.Column("proposal", postgresql.JSONB(), nullable=True),
        sa.Column("planned_count", sa.Integer(), nullable=True),
        sa.Column("answered_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "preparation_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("evaluation_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_interview_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resume_id"],
            ["resumes.id"],
            name=op.f("fk_interview_sessions_resume_id_resumes"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interview_sessions")),
    )
    op.create_index(
        op.f("ix_interview_sessions_user_id"), "interview_sessions", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_interview_sessions_resume_id"), "interview_sessions", ["resume_id"], unique=False
    )
    op.create_index(
        "ux_one_open_session_per_user",
        "interview_sessions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('cancelled', 'completed', 'expired')"),
    )

    op.create_table(
        "questions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("skill_name", sa.String(length=255), nullable=False),
        sa.Column("expected_level", expected_level, nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "reference_points", postgresql.JSONB(), server_default=EMPTY_JSON_ARRAY, nullable=False
        ),
        sa.Column("sources", postgresql.JSONB(), server_default=EMPTY_JSON_ARRAY, nullable=False),
        sa.Column("no_verified_source", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["interview_sessions.id"],
            name=op.f("fk_questions_session_id_interview_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_questions")),
        sa.UniqueConstraint("session_id", "position", name=op.f("uq_questions_session_id")),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("role", message_role, nullable=False),
        sa.Column("kind", message_kind, nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["interview_sessions.id"],
            name=op.f("fk_messages_session_id_interview_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["questions.id"],
            name=op.f("fk_messages_question_id_questions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_index(
        "ix_messages_session_id_created_at",
        "messages",
        ["session_id", "created_at"],
        unique=False,
    )
    op.create_index(op.f("ix_messages_question_id"), "messages", ["question_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_messages_question_id"), table_name="messages")
    op.drop_index("ix_messages_session_id_created_at", table_name="messages")
    op.drop_table("messages")
    op.drop_table("questions")
    op.drop_index("ux_one_open_session_per_user", table_name="interview_sessions")
    op.drop_index(op.f("ix_interview_sessions_resume_id"), table_name="interview_sessions")
    op.drop_index(op.f("ix_interview_sessions_user_id"), table_name="interview_sessions")
    op.drop_table("interview_sessions")
    bind = op.get_bind()
    for enum_type in reversed(ENUMS):
        enum_type.drop(bind, checkfirst=True)
