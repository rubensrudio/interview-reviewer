"""Knowledge base items (`knowledge_items`, plan 7.4) and the `SourceRef` citation schema.

Retrieval uses a generated `tsvector` column with a GIN index (DA-7); no embeddings.
The table holds public technical content only, never user data.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import CheckConstraint, Computed, DateTime, Index, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

EXCERPT_MAX_LENGTH = 2000

# `array_to_string` is only STABLE, so migration 0004 wraps it in the IMMUTABLE SQL function
# `knowledge_terms_text(text[])`, which a generated column is allowed to call.
SEARCH_EXPRESSION = (
    "to_tsvector('english', title || ' ' || excerpt || ' ' || knowledge_terms_text(skill_terms))"
)


class SourceRef(BaseModel):
    """Citation of a knowledge item, copied (frozen) into questions and reports (KNOW-08)."""

    model_config = ConfigDict(extra="forbid")

    url: str
    title: str
    collected_at: datetime
    excerpt: str = Field(max_length=EXCERPT_MAX_LENGTH)


class KnowledgeItem(Base):
    __tablename__ = "knowledge_items"
    __table_args__ = (
        CheckConstraint(f"char_length(excerpt) <= {EXCERPT_MAX_LENGTH}", name="excerpt_max_length"),
        Index("ix_knowledge_items_search", "search", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    excerpt: Mapped[str] = mapped_column(Text)
    skill_terms: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    search: Mapped[str] = mapped_column(TSVECTOR, Computed(SEARCH_EXPRESSION, persisted=True))

    def to_source_ref(self) -> SourceRef:
        return SourceRef(
            url=self.url, title=self.title, collected_at=self.collected_at, excerpt=self.excerpt
        )
