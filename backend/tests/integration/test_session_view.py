"""Tests for the safe interview session projection (CT-40, INTV-01, INTV-10, INTV-14)."""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.interviews.views import SessionView, build_session_view
from app.models import User
from app.models.assessment import Answer, Report
from app.models.interview import (
    InterviewSession,
    Message,
    MessageKind,
    MessageRole,
    Question,
    SessionStatus,
)

REFERENCE_MARKERS = [
    "REFPOINT-alpha-7f3c",
    "REFPOINT-beta-91d2",
    "REFPOINT-gamma-4ab0",
]
SOURCE_MARKER = "SOURCE-URL-marker-5e1d"

REQUIREMENT_ITEMS: list[dict[str, Any]] = [
    {
        "id": "r1",
        "name": "Python",
        "original_terms": ["Python 3"],
        "classification": "required",
        "level": "senior",
        "pending_clarification": False,
        "clarification_question": None,
    },
    {
        "id": "r2",
        "name": "Docker",
        "original_terms": ["Docker"],
        "classification": "nice_to_have",
        "level": None,
        "pending_clarification": False,
        "clarification_question": None,
    },
]


def _user(db: Session) -> User:
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    return user


def _interview(db: Session, *, planned: int = 3, answered: int = 1) -> InterviewSession:
    session = InterviewSession(
        user_id=_user(db).id,
        status=SessionStatus.IN_INTERVIEW,
        resume_name="resume.pdf",
        requirement_items=REQUIREMENT_ITEMS,
        non_technical=["Good communication"],
        proposal={"planned_count": planned, "skills": ["Python"]},
        planned_count=planned,
        answered_count=answered,
    )
    db.add(session)
    db.flush()
    db.add(
        Message(
            session_id=session.id,
            role=MessageRole.CANDIDATE,
            kind=MessageKind.REQUIREMENTS,
            content="We need Python and Docker.",
        )
    )
    questions = [
        Question(
            session_id=session.id,
            position=position,
            skill_name="Python",
            text=f"Question number {position}?",
            reference_points=[REFERENCE_MARKERS[(position - 1) % len(REFERENCE_MARKERS)]],
            sources=[
                {
                    "url": f"https://docs.example.com/{SOURCE_MARKER}",
                    "title": SOURCE_MARKER,
                }
            ],
        )
        for position in range(1, planned + 1)
    ]
    db.add_all(questions)
    db.flush()
    for question in questions[:answered]:
        db.add(
            Answer(
                session_id=session.id,
                question_id=question.id,
                content=f"My answer to {question.position}.",
                idempotency_key=uuid.uuid4().hex,
            )
        )
    db.flush()
    return session


def test_counter_and_current_question_in_interview_intv_01(db: Session) -> None:
    session = _interview(db, planned=3, answered=1)

    view = build_session_view(db, session)

    assert isinstance(view, SessionView)
    assert view.status == SessionStatus.IN_INTERVIEW
    assert view.counter is not None
    assert view.counter.model_dump() == {"planned": 3, "answered": 1, "remaining": 2}
    assert view.current_question is not None
    assert view.current_question.position == 2
    assert view.current_question.skill == "Python"
    assert view.current_question.text == "Question number 2?"


def test_answered_items_and_requirements_intv_10(db: Session) -> None:
    session = _interview(db, planned=3, answered=2)

    view = build_session_view(db, session)

    assert [item.position for item in view.answered] == [1, 2]
    assert view.answered[0].question == "Question number 1?"
    assert view.answered[0].answer == "My answer to 1."
    assert view.current_question is not None and view.current_question.position == 3
    assert view.requirements is not None
    assert [item.name for item in view.requirements.items] == ["Python", "Docker"]
    assert view.requirements.non_technical == ["Good communication"]
    assert view.proposal is not None and view.proposal.planned_count == 3
    assert [message.kind for message in view.messages] == [MessageKind.REQUIREMENTS]
    assert view.report_available is False


def test_serialized_view_never_contains_reference_points_intv_14(db: Session) -> None:
    session = _interview(db, planned=3, answered=1)

    body = build_session_view(db, session).model_dump_json()

    for marker in REFERENCE_MARKERS:
        assert marker not in body
    assert SOURCE_MARKER not in body
    assert "reference_points" not in body
    assert "sources" not in body


def test_view_is_idempotent_and_has_no_side_effects_intv_10(db: Session) -> None:
    session = _interview(db, planned=3, answered=1)
    answers_before = db.scalar(select(func.count()).select_from(Answer))

    first = build_session_view(db, session).model_dump_json()
    second = build_session_view(db, session).model_dump_json()

    assert first == second
    assert not db.dirty and not db.new and not db.deleted
    assert db.scalar(select(func.count()).select_from(Answer)) == answers_before
    assert session.answered_count == 1
    assert session.status == SessionStatus.IN_INTERVIEW


def test_answers_from_another_session_are_ignored_intv_10(db: Session) -> None:
    session = _interview(db, planned=3, answered=0)
    other = _interview(db, planned=1, answered=0)
    foreign_question = db.scalars(select(Question).where(Question.session_id == other.id)).one()
    # Inconsistent row the database does not reject: answer tied to this session but to a
    # question of another session.
    db.add(
        Answer(
            session_id=session.id,
            question_id=foreign_question.id,
            content="Stray answer.",
            idempotency_key=uuid.uuid4().hex,
        )
    )
    db.flush()

    view = build_session_view(db, session)

    assert view.answered == []
    assert view.counter is not None and view.counter.answered == 0
    assert view.current_question is not None and view.current_question.position == 1
    assert "Stray answer." not in view.model_dump_json()


def test_no_current_question_outside_interview(db: Session) -> None:
    session = _interview(db, planned=2, answered=2)
    session.status = SessionStatus.EVALUATING
    db.flush()

    view = build_session_view(db, session)

    assert view.current_question is None
    assert view.counter is not None
    assert view.counter.model_dump() == {"planned": 2, "answered": 2, "remaining": 0}


def test_collecting_session_has_empty_projection(db: Session) -> None:
    session = InterviewSession(user_id=_user(db).id)
    db.add(session)
    db.flush()
    db.refresh(session)

    view = build_session_view(db, session)

    assert view.status == SessionStatus.COLLECTING_REQUIREMENTS
    assert view.requirements is None
    assert view.proposal is None
    assert view.counter is None
    assert view.current_question is None
    assert view.answered == []
    assert view.messages == []
    assert view.report_available is False


def test_report_available_only_when_completed_with_report(db: Session) -> None:
    session = _interview(db, planned=1, answered=1)
    session.status = SessionStatus.COMPLETED
    db.add(
        Report(
            session_id=session.id,
            content={"summary": "REPORT-CONTENT-marker"},
            adherence_percentage=50,
            model_version="m",
            rubric_version="r",
        )
    )
    db.flush()

    view = build_session_view(db, session)

    assert view.report_available is True
    assert "REPORT-CONTENT-marker" not in view.model_dump_json()
