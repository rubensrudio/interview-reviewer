"""Interview session state machine (spec section 7.2, CT-35).

Only the status graph is enforced here. Business preconditions (answered count, valid
evaluations, list validation) belong to the callers that fire each transition.
"""

from datetime import UTC, datetime

from app.errors import INVALID_STATE, AppError
from app.models.interview import TERMINAL_STATUSES, InterviewSession, SessionStatus

__all__ = [
    "AWAITING_CANDIDATE_STATUSES",
    "TERMINAL_STATUSES",
    "can_transition",
    "transition",
]

# States waiting on the candidate; after 30 idle days they expire (INTV-13).
AWAITING_CANDIDATE_STATUSES: frozenset[SessionStatus] = frozenset(
    {
        SessionStatus.COLLECTING_REQUIREMENTS,
        SessionStatus.AWAITING_CONFIRMATION,
        SessionStatus.IN_INTERVIEW,
        SessionStatus.PREPARATION_FAILED,
        SessionStatus.EVALUATION_FAILED,
    }
)

_NON_TERMINAL_STATUSES: frozenset[SessionStatus] = frozenset(
    status for status in SessionStatus if status not in TERMINAL_STATUSES
)

# Explicit rows of table 7.2 (cancel and expiry are added below).
_TABLE: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.COLLECTING_REQUIREMENTS: frozenset({SessionStatus.AWAITING_CONFIRMATION}),
    SessionStatus.AWAITING_CONFIRMATION: frozenset(
        {SessionStatus.AWAITING_CONFIRMATION, SessionStatus.PREPARING_QUESTIONS}
    ),
    SessionStatus.PREPARING_QUESTIONS: frozenset(
        {SessionStatus.IN_INTERVIEW, SessionStatus.PREPARATION_FAILED}
    ),
    SessionStatus.PREPARATION_FAILED: frozenset({SessionStatus.PREPARING_QUESTIONS}),
    SessionStatus.IN_INTERVIEW: frozenset({SessionStatus.IN_INTERVIEW, SessionStatus.EVALUATING}),
    SessionStatus.EVALUATING: frozenset({SessionStatus.COMPLETED, SessionStatus.EVALUATION_FAILED}),
    SessionStatus.EVALUATION_FAILED: frozenset({SessionStatus.EVALUATING}),
}


def _build_transitions() -> dict[SessionStatus, frozenset[SessionStatus]]:
    allowed: dict[SessionStatus, frozenset[SessionStatus]] = {}
    for status in SessionStatus:
        targets = set(_TABLE.get(status, frozenset()))
        if status in _NON_TERMINAL_STATUSES:
            targets.add(SessionStatus.CANCELLED)  # INTV-12
        if status in AWAITING_CANDIDATE_STATUSES:
            targets.add(SessionStatus.EXPIRED)  # INTV-13
        # Terminal states never leave (INTV-12, INTV-13, INTV-93, EVAL-12).
        allowed[status] = frozenset() if status in TERMINAL_STATUSES else frozenset(targets)
    return allowed


_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = _build_transitions()


def can_transition(frm: SessionStatus, to: SessionStatus) -> bool:
    """Return whether table 7.2 allows moving a session from `frm` to `to`."""
    return to in _TRANSITIONS.get(frm, frozenset())


def transition(session: InterviewSession, to: SessionStatus) -> None:
    """Move `session` to `to`, or raise `INVALID_STATE` (409) without changing it."""
    if not can_transition(session.status, to):
        raise AppError.from_catalog(INVALID_STATE)
    session.status = to
    if to == SessionStatus.COMPLETED:
        session.completed_at = datetime.now(UTC)
