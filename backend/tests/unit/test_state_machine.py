"""Interview session state machine (spec 7.2, CT-35)."""

from datetime import UTC, datetime

import pytest

from app.errors import INVALID_STATE, AppError
from app.interviews.state_machine import (
    AWAITING_CANDIDATE_STATUSES,
    TERMINAL_STATUSES,
    can_transition,
    transition,
)
from app.models.interview import InterviewSession, SessionStatus

S = SessionStatus

NON_TERMINAL = [s for s in S if s not in {S.COMPLETED, S.CANCELLED, S.EXPIRED}]
AWAITING = [
    S.COLLECTING_REQUIREMENTS,
    S.AWAITING_CONFIRMATION,
    S.IN_INTERVIEW,
    S.PREPARATION_FAILED,
    S.EVALUATION_FAILED,
]

# Every row of spec table 7.2 (creation and deletion are not status transitions).
SPEC_TRANSITIONS: set[tuple[SessionStatus, SessionStatus]] = {
    (S.COLLECTING_REQUIREMENTS, S.AWAITING_CONFIRMATION),
    (S.AWAITING_CONFIRMATION, S.AWAITING_CONFIRMATION),
    (S.AWAITING_CONFIRMATION, S.PREPARING_QUESTIONS),
    (S.PREPARING_QUESTIONS, S.IN_INTERVIEW),
    (S.PREPARING_QUESTIONS, S.PREPARATION_FAILED),
    (S.PREPARATION_FAILED, S.PREPARING_QUESTIONS),
    (S.IN_INTERVIEW, S.IN_INTERVIEW),
    (S.IN_INTERVIEW, S.EVALUATING),
    (S.EVALUATING, S.COMPLETED),
    (S.EVALUATING, S.EVALUATION_FAILED),
    (S.EVALUATION_FAILED, S.EVALUATING),
    *((frm, S.CANCELLED) for frm in NON_TERMINAL),
    *((frm, S.EXPIRED) for frm in AWAITING),
}


def _session(status: SessionStatus) -> InterviewSession:
    return InterviewSession(status=status)


@pytest.mark.parametrize(("frm", "to"), sorted(SPEC_TRANSITIONS))
def test_every_spec_transition_is_accepted(frm: SessionStatus, to: SessionStatus) -> None:
    session = _session(frm)

    assert can_transition(frm, to) is True
    transition(session, to)

    assert session.status == to


@pytest.mark.parametrize(
    ("frm", "to"),
    sorted((frm, to) for frm in S for to in S if (frm, to) not in SPEC_TRANSITIONS),
)
def test_every_transition_outside_the_table_is_rejected(
    frm: SessionStatus, to: SessionStatus
) -> None:
    session = _session(frm)

    assert can_transition(frm, to) is False
    with pytest.raises(AppError) as exc_info:
        transition(session, to)

    assert exc_info.value.code == INVALID_STATE
    assert exc_info.value.status == 409
    assert session.status == frm


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (S.COMPLETED, S.EVALUATING),  # EVAL-12
        (S.CANCELLED, S.IN_INTERVIEW),  # INTV-12
        (S.EXPIRED, S.COLLECTING_REQUIREMENTS),  # INTV-13
    ],
)
def test_terminal_states_cannot_be_left(frm: SessionStatus, to: SessionStatus) -> None:
    session = _session(frm)

    with pytest.raises(AppError) as exc_info:
        transition(session, to)

    assert exc_info.value.code == INVALID_STATE
    assert exc_info.value.status == 409
    assert exc_info.value.message == "This action is not available at this step."
    assert session.status == frm


@pytest.mark.parametrize("terminal", [S.COMPLETED, S.CANCELLED, S.EXPIRED])
def test_terminal_states_have_no_outgoing_transition(terminal: SessionStatus) -> None:
    # INTV-93: nothing (answer, clarification, cancel, expiry) moves a closed session.
    assert not any(can_transition(terminal, to) for to in S)


def test_awaiting_candidate_statuses_are_exactly_the_spec_set() -> None:
    assert AWAITING_CANDIDATE_STATUSES == {
        S.COLLECTING_REQUIREMENTS,
        S.AWAITING_CONFIRMATION,
        S.IN_INTERVIEW,
        S.PREPARATION_FAILED,
        S.EVALUATION_FAILED,
    }
    assert isinstance(AWAITING_CANDIDATE_STATUSES, frozenset)


def test_terminal_statuses_are_completed_cancelled_expired() -> None:
    assert TERMINAL_STATUSES == {S.COMPLETED, S.CANCELLED, S.EXPIRED}


def test_entering_completed_sets_completed_at() -> None:
    session = _session(S.EVALUATING)
    before = datetime.now(UTC)

    transition(session, S.COMPLETED)

    assert session.completed_at is not None
    assert before <= session.completed_at <= datetime.now(UTC)
    assert session.completed_at.tzinfo is not None


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (S.IN_INTERVIEW, S.EVALUATING),
        (S.EVALUATING, S.EVALUATION_FAILED),
        (S.IN_INTERVIEW, S.CANCELLED),
        (S.IN_INTERVIEW, S.EXPIRED),
    ],
)
def test_other_transitions_do_not_set_completed_at(frm: SessionStatus, to: SessionStatus) -> None:
    session = _session(frm)

    transition(session, to)

    assert session.completed_at is None


def test_rejected_transition_does_not_touch_completed_at() -> None:
    session = _session(S.IN_INTERVIEW)

    with pytest.raises(AppError):
        transition(session, S.COMPLETED)

    assert session.completed_at is None
