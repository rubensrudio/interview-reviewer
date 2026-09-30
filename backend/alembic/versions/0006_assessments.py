"""assessments

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-30 00:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMPTY_JSON_ARRAY = sa.text("'[]'::jsonb")

# Accepted answers and completed reports can never be edited (INTV-09, EVAL-12).
IMMUTABLE_TABLES = ("answers", "reports")


def _create_immutable_trigger(table: str) -> None:
    name = f"{table}_immutable"
    op.execute(
        f"""
        CREATE FUNCTION {name}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '{name}: rows of {table} cannot be updated';
        END;
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION {name}()"
    )


def _drop_immutable_trigger(table: str) -> None:
    name = f"{table}_immutable"
    op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    op.execute(f"DROP FUNCTION IF EXISTS {name}()")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "answers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["interview_sessions.id"],
            name=op.f("fk_answers_session_id_interview_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["questions.id"],
            name=op.f("fk_answers_question_id_questions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_answers")),
        sa.UniqueConstraint("question_id", name=op.f("uq_answers_question_id")),
        sa.UniqueConstraint("session_id", "idempotency_key", name=op.f("uq_answers_session_id")),
    )

    op.create_table(
        "evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("answer_id", sa.Uuid(), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column(
            "evidence_quotes", postgresql.JSONB(), server_default=EMPTY_JSON_ARRAY, nullable=False
        ),
        sa.Column("gap_explanation", sa.Text(), nullable=True),
        sa.Column("reference_answer", postgresql.JSONB(), nullable=True),
        sa.Column("model_version", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("score BETWEEN 0 AND 4", name=op.f("ck_evaluations_score_range")),
        sa.ForeignKeyConstraint(
            ["answer_id"],
            ["answers.id"],
            name=op.f("fk_evaluations_answer_id_answers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluations")),
        sa.UniqueConstraint("answer_id", name=op.f("uq_evaluations_answer_id")),
    )

    op.create_table(
        "reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("adherence_percentage", sa.Numeric(precision=4, scale=1), nullable=False),
        sa.Column("model_version", sa.String(length=128), nullable=False),
        sa.Column("rubric_version", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["interview_sessions.id"],
            name=op.f("fk_reports_session_id_interview_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reports")),
        sa.UniqueConstraint("session_id", name=op.f("uq_reports_session_id")),
    )

    for table in IMMUTABLE_TABLES:
        _create_immutable_trigger(table)


def downgrade() -> None:
    """Downgrade schema."""
    for table in reversed(IMMUTABLE_TABLES):
        _drop_immutable_trigger(table)
    op.drop_table("reports")
    op.drop_table("evaluations")
    op.drop_table("answers")
