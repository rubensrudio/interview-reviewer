"""resumes

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-30 00:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

resume_status = postgresql.ENUM(
    "received", "processing", "ready", "failed", name="resume_status", create_type=False
)


def upgrade() -> None:
    """Upgrade schema."""
    resume_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "resumes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column(
            "uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("status", resume_status, server_default="received", nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("storage_key", sa.String(length=255), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("extraction", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_resumes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resumes")),
    )
    op.create_index(
        "ix_resumes_user_id_uploaded_at",
        "resumes",
        ["user_id", sa.literal_column("uploaded_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_resumes_user_id_uploaded_at", table_name="resumes")
    op.drop_table("resumes")
    resume_status.drop(op.get_bind(), checkfirst=True)
