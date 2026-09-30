"""Safe projection of an interview session (CT-40, body of `GET /api/sessions/{id}`).

Every session route answers with `SessionView`. The schema has no field for a question's
reference points, expected answer or sources, so none of them can leak before the report
(INTV-14). Building the view only reads: it never changes the session or its rows (INTV-10).
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.assessment import Answer, Report
from app.models.interview import (
    ExpectedLevel,
    InterviewSession,
    Message,
    MessageKind,
    MessageRole,
    Question,
    RequirementItem,
    SessionStatus,
)

__all__ = [
    "AnsweredItemView",
    "CounterView",
    "CurrentQuestionView",
    "MessageView",
    "ProposalView",
    "RequirementsView",
    "SessionView",
    "build_session_view",
]


class _ViewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MessageView(_ViewModel):
    id: uuid.UUID
    role: MessageRole
    kind: MessageKind
    content: str
    created_at: datetime


class RequirementsView(_ViewModel):
    items: list[RequirementItem]
    non_technical: list[str]


class ProposalView(_ViewModel):
    planned_count: int
    skills: list[str]


class CounterView(_ViewModel):
    planned: int
    answered: int
    remaining: int


class CurrentQuestionView(_ViewModel):
    id: uuid.UUID
    position: int
    skill: str
    text: str


class AnsweredItemView(_ViewModel):
    question_id: uuid.UUID
    position: int
    skill: str
    question: str
    answer: str


class SessionView(_ViewModel):
    id: uuid.UUID
    status: SessionStatus
    created_at: datetime
    language: str
    interview_level: ExpectedLevel | None
    resume_name: str | None
    messages: list[MessageView]
    requirements: RequirementsView | None
    proposal: ProposalView | None
    counter: CounterView | None
    current_question: CurrentQuestionView | None
    answered: list[AnsweredItemView]
    report_available: bool


def _messages(db: Session, session_id: uuid.UUID) -> list[MessageView]:
    rows = db.execute(
        select(Message.id, Message.role, Message.kind, Message.content, Message.created_at)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at, Message.id)
    ).all()
    return [
        MessageView(
            id=row.id, role=row.role, kind=row.kind, content=row.content, created_at=row.created_at
        )
        for row in rows
    ]


def _requirements(session: InterviewSession) -> RequirementsView | None:
    items = list(session.requirement_items or [])
    non_technical = list(session.non_technical or [])
    if not items and not non_technical:
        return None
    return RequirementsView(
        items=[RequirementItem.model_validate(item) for item in items],
        non_technical=[str(entry) for entry in non_technical],
    )


def _proposal(session: InterviewSession) -> ProposalView | None:
    if not session.proposal:
        return None
    return ProposalView(
        planned_count=int(session.proposal["planned_count"]),
        skills=[str(skill) for skill in session.proposal.get("skills", [])],
    )


def build_session_view(db: Session, session: InterviewSession) -> SessionView:
    """Project `session` into the only shape the API exposes (CT-40)."""
    # Only the columns the view needs are selected; reference points and sources are never
    # loaded. Both sides are filtered by session so an answer tied to a question of another
    # session is ignored (the database does not enforce that pairing).
    questions = db.execute(
        select(Question.id, Question.position, Question.skill_name, Question.text)
        .where(Question.session_id == session.id)
        .order_by(Question.position)
    ).all()
    answer_rows = db.execute(
        select(Answer.question_id, Answer.content)
        .join(Question, Question.id == Answer.question_id)
        .where(Answer.session_id == session.id, Question.session_id == session.id)
    ).all()
    answers = {row.question_id: row.content for row in answer_rows}

    answered = [
        AnsweredItemView(
            question_id=question.id,
            position=question.position,
            skill=question.skill_name,
            question=question.text,
            answer=answers[question.id],
        )
        for question in questions
        if question.id in answers
    ]

    counter: CounterView | None = None
    if session.planned_count is not None:
        planned = session.planned_count
        counter = CounterView(
            planned=planned, answered=len(answered), remaining=max(planned - len(answered), 0)
        )

    current_question: CurrentQuestionView | None = None
    if session.status == SessionStatus.IN_INTERVIEW:
        pending = next((q for q in questions if q.id not in answers), None)
        if pending is not None:
            current_question = CurrentQuestionView(
                id=pending.id,
                position=pending.position,
                skill=pending.skill_name,
                text=pending.text,
            )

    report_available = session.status == SessionStatus.COMPLETED and (
        db.scalar(select(Report.id).where(Report.session_id == session.id)) is not None
    )

    return SessionView(
        id=session.id,
        status=session.status,
        created_at=session.created_at,
        language=session.language,
        interview_level=session.interview_level,
        resume_name=session.resume_name,
        messages=_messages(db, session.id),
        requirements=_requirements(session),
        proposal=_proposal(session),
        counter=counter,
        current_question=current_question,
        answered=answered,
        report_available=report_available,
    )
