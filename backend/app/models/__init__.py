"""ORM models. Every model module must be imported here so Alembic sees its tables."""

from app.db import Base
from app.models.account import AuthSession, LoginThrottle, OneTimeToken, TokenPurpose, User
from app.models.assessment import Answer, Evaluation, Report
from app.models.interview import (
    ExpectedLevel,
    InterviewSession,
    Message,
    MessageKind,
    Question,
    RequirementItem,
    SessionStatus,
)
from app.models.job import Job, JobStatus
from app.models.knowledge import KnowledgeItem, SourceRef
from app.models.resume import ExtractionItem, Resume, ResumeStatus

__all__ = [
    "Answer",
    "AuthSession",
    "Base",
    "Evaluation",
    "ExpectedLevel",
    "ExtractionItem",
    "InterviewSession",
    "Job",
    "JobStatus",
    "KnowledgeItem",
    "LoginThrottle",
    "Message",
    "MessageKind",
    "OneTimeToken",
    "Question",
    "Report",
    "RequirementItem",
    "Resume",
    "ResumeStatus",
    "SessionStatus",
    "SourceRef",
    "TokenPurpose",
    "User",
]
