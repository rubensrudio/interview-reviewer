"""Resume versions (`resumes`, plan 7.3) and the pydantic schema of each extracted item.

The extraction is stored as JSONB (DA-13): a list of `ExtractionItem` dumps, never a
separate table. `filename`, `extracted_text` and `extraction` are PII.
"""

import enum
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ResumeStatus(enum.StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


ExtractionKind = Literal["experience", "education", "skill"]
ExtractionOrigin = Literal["explicit", "inferred", "user_provided"]


class ExtractionItem(BaseModel):
    """One skill, experience or education entry of a resume extraction.

    `fields` keys: title, organization, start, end, degree, institution, name, description.
    Items read from the resume (`explicit`/`inferred`) must quote evidence from its text;
    only items entered by the candidate (`user_provided`) may have no evidence.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: ExtractionKind
    fields: dict[str, str]
    origin: ExtractionOrigin
    evidence: list[str]

    @model_validator(mode="after")
    def _evidence_required_unless_user_provided(self) -> "ExtractionItem":
        if not self.evidence and self.origin != "user_provided":
            raise ValueError("evidence may be empty only for user_provided items")
        return self


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # PII.
    filename: Mapped[str] = mapped_column(String(255))
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[ResumeStatus] = mapped_column(
        Enum(
            ResumeStatus,
            name="resume_status",
            values_callable=lambda members: [member.value for member in members],
        ),
        default=ResumeStatus.RECEIVED,
        server_default=ResumeStatus.RECEIVED.value,
    )
    # Machine code only (e.g. NOT_ENGLISH, CORRUPTED); never a message with user data.
    failure_code: Mapped[str | None] = mapped_column(String(64))
    storage_key: Mapped[str | None] = mapped_column(String(255))
    # PII.
    extracted_text: Mapped[str | None] = mapped_column(Text)
    # PII. List of `ExtractionItem.model_dump(mode="json")`.
    extraction: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB(none_as_null=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


Index("ix_resumes_user_id_uploaded_at", Resume.user_id, Resume.uploaded_at.desc())
