"""Expiry of interview sessions left idle by the candidate (INTV-13, CT-43).

A session in one of the states that wait on the candidate moves to the terminal ``expired``
state once its ``last_activity_at`` is older than ``session_expiry_days``. Expired sessions are
kept (retention follows user deletion) and no report is generated for them.

The periodic runs in every worker process without deduplication, so it must be idempotent:
rows are locked with ``FOR UPDATE SKIP LOCKED`` in id order, the filter is re-checked on the
locked row, and a session that is already expired no longer matches. A session locked by a
concurrent user action is skipped and reconsidered on the next run.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.interviews.state_machine import AWAITING_CANDIDATE_STATUSES, can_transition, transition
from app.models.interview import InterviewSession, SessionStatus
from app.observability import log_event

__all__ = [
    "EXPIRE_PERIODIC_NAME",
    "EXPIRE_PERIODIC_SECONDS",
    "expire_inactive_sessions",
    "run_session_expiry",
]

EXPIRE_PERIODIC_NAME = "session.expire"
EXPIRE_PERIODIC_SECONDS = 3600


def expire_inactive_sessions(db: Session, now: datetime) -> int:
    """Move idle sessions awaiting the candidate to ``expired``; return how many moved.

    ``now`` must be timezone-aware. The caller commits.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    cutoff = now - timedelta(days=get_settings().session_expiry_days)
    statement = (
        select(InterviewSession)
        .where(
            InterviewSession.status.in_(AWAITING_CANDIDATE_STATUSES),
            InterviewSession.last_activity_at < cutoff,
        )
        .order_by(InterviewSession.id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    expired = 0
    for session in db.execute(statement).scalars():
        if session.status not in AWAITING_CANDIDATE_STATUSES or session.last_activity_at >= cutoff:
            continue
        if not can_transition(session.status, SessionStatus.EXPIRED):
            continue
        transition(session, SessionStatus.EXPIRED)
        expired += 1
    if expired:
        db.flush()
        log_event("session.expired", count=expired)
    return expired


def run_session_expiry(db: Session) -> None:
    """Periodic entry point (``session.expire``): expire sessions idle as of the current time."""
    expire_inactive_sessions(db, datetime.now(UTC))
