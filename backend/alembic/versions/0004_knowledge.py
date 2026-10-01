"""knowledge

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30 00:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Generated columns may only call IMMUTABLE functions; `array_to_string` is STABLE, so it is
# wrapped here. Safe because the argument is always text[] (no type-dependent output).
CREATE_TERMS_FUNCTION = """
CREATE FUNCTION knowledge_terms_text(terms text[]) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$ SELECT coalesce(array_to_string(terms, ' '), '') $$
"""

SEARCH_EXPRESSION = (
    "to_tsvector('english', title || ' ' || excerpt || ' ' || knowledge_terms_text(skill_terms))"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(CREATE_TERMS_FUNCTION)

    op.create_table(
        "knowledge_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column(
            "skill_terms",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed(SEARCH_EXPRESSION, persisted=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(excerpt) <= 2000",
            name=op.f("ck_knowledge_items_excerpt_max_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_items")),
        sa.UniqueConstraint("url", name=op.f("uq_knowledge_items_url")),
    )
    op.create_index(
        "ix_knowledge_items_search",
        "knowledge_items",
        ["search"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_knowledge_items_search", table_name="knowledge_items")
    op.drop_table("knowledge_items")
    op.execute("DROP FUNCTION IF EXISTS knowledge_terms_text(text[])")
