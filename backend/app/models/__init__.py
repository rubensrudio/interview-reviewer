"""ORM models. Every model module must be imported here so Alembic sees its tables."""

from app.db import Base
from app.models.account import AuthSession, LoginThrottle, OneTimeToken, TokenPurpose, User
from app.models.job import Job, JobStatus
from app.models.knowledge import KnowledgeItem, SourceRef
from app.models.resume import ExtractionItem, Resume, ResumeStatus

__all__ = [
    "AuthSession",
    "Base",
    "ExtractionItem",
    "Job",
    "JobStatus",
    "KnowledgeItem",
    "LoginThrottle",
    "OneTimeToken",
    "Resume",
    "ResumeStatus",
    "SourceRef",
    "TokenPurpose",
    "User",
]
