"""Answers, evaluations and the final report (plan 7.6).

Accepted answers and completed reports are immutable: the database triggers
`answers_immutable` and `reports_immutable` (migration 0006) reject any UPDATE (INTV-09,
EVAL-12). An answer is unique per question and per `(session_id, idempotency_key)`, so a
resubmitted request never records a second answer (INTV-06).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

SCORE_MIN = 0
SCORE_MAX = 4


class Answer(Base):
    __tablename__ = "answers"
    # The unique (session_id, idempotency_key) index also serves lookups by session.
    __table_args__ = (UniqueConstraint("session_id", "idempotency_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE")
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), unique=True
    )
    # PII.
    content: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Evaluation(Base):
    __tablename__ = "evaluations"
    __table_args__ = (
        CheckConstraint(f"score BETWEEN {SCORE_MIN} AND {SCORE_MAX}", name="score_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    answer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("answers.id", ondelete="CASCADE"), unique=True
    )
    score: Mapped[int] = mapped_column(SmallInteger)
    justification: Mapped[str] = mapped_column(Text)
    # PII: literal excerpts of the candidate's answer.
    evidence_quotes: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    gap_explanation: Mapped[str | None] = mapped_column(Text)
    # `{text, points, sources: [SourceRef], hypothetical_example, example_text}` or NULL.
    reference_answer: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    model_version: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE"), unique=True
    )
    # PII. Frozen `ReportContent` (plan 7.6), serialized with `model_dump(mode="json")`.
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    adherence_percentage: Mapped[Decimal] = mapped_column(Numeric(4, 1))
    model_version: Mapped[str] = mapped_column(String(128))
    rubric_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
