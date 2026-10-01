"""Tests for clarification requests during the interview (CT-42, INTV-08, INTV-92, INTV-93)."""

import uuid

import pytest
from fakes.fake_llm import FakeLLM
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import (
    CLARIFICATION_UNAVAILABLE,
    INVALID_STATE,
    SESSION_CLOSED,
    VALIDATION_ERROR,
    AppError,
)
from app.interviews.clarification import (
    CLARIFICATION_REFUSAL,
    CLARIFICATION_TASK,
    request_clarification,
)
from app.interviews.views import build_session_view
from app.llm.client import LLMInvalidOutput, LLMUnavailable
from app.models import User
from app.models.assessment import Answer
from app.models.interview import (
    InterviewSession,
    Message,
    MessageKind,
    MessageRole,
    Question,
    SessionStatus,
)

REFERENCE_POINTS = [
    "Explain the global interpreter lock limits CPU bound threads",
    "Mention multiprocessing as the workaround for parallelism",
]
FIRST_QUESTION = "How does Python handle concurrency in CPU-bound workloads?"
SECOND_QUESTION = "How would you design a REST API for pagination?"
SECOND_REFERENCE_POINTS = ["Cursor based pagination keeps stable ordering under inserts"]
DOUBT = "Do you mean threads or async code?"


def _user(db: Session) -> User:
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    return user


def _interview(
    db: Session, *, status: SessionStatus = SessionStatus.IN_INTERVIEW, answer_first: bool = True
) -> tuple[InterviewSession, Question]:
    session = InterviewSession(
        user_id=_user(db).id,
        status=status,
        planned_count=2,
        answered_count=0,
        proposal={"planned_count": 2, "skills": ["Python", "APIs"]},
    )
    db.add(session)
    db.flush()
    first = Question(
        session_id=session.id,
        position=1,
        skill_name="Python",
        text=FIRST_QUESTION,
        reference_points=REFERENCE_POINTS,
    )
    second = Question(
        session_id=session.id,
        position=2,
        skill_name="APIs",
        text=SECOND_QUESTION,
        reference_points=SECOND_REFERENCE_POINTS,
    )
    db.add_all([first, second])
    db.flush()
    if not answer_first:
        return session, first
    db.add(
        Answer(
            session_id=session.id,
            question_id=first.id,
            content="I would use multiprocessing.",
            idempotency_key="k1",
        )
    )
    session.answered_count = 1
    db.flush()
    return session, second


def _messages(db: Session, session: InterviewSession) -> list[Message]:
    # Same order as the session view (CT-40).
    return list(
        db.execute(
            select(Message)
            .where(Message.session_id == session.id)
            .order_by(Message.created_at, Message.id)
        )
        .scalars()
        .all()
    )


def _answers(db: Session, session: InterviewSession) -> int:
    return len(db.execute(select(Answer.id).where(Answer.session_id == session.id)).all())


def _current_question_id(db: Session, session: InterviewSession) -> uuid.UUID | None:
    answered = select(Answer.question_id).where(Answer.session_id == session.id)
    return db.execute(
        select(Question.id)
        .where(Question.session_id == session.id, Question.id.not_in(answered))
        .order_by(Question.position)
        .limit(1)
    ).scalar_one_or_none()


def test_intv08_clarification_keeps_counter_and_current_question(db: Session) -> None:
    session, current = _interview(db)
    before_count = session.answered_count
    before_question = _current_question_id(db, session)
    llm = FakeLLM({CLARIFICATION_TASK: [{"reply": "Either is fine; focus on the trade-offs."}]})

    reply = request_clarification(db, llm, session, DOUBT)

    db.expire_all()
    refreshed = db.get(InterviewSession, session.id)
    assert refreshed is not None
    assert refreshed.answered_count == before_count
    assert refreshed.status == SessionStatus.IN_INTERVIEW
    assert _current_question_id(db, refreshed) == before_question == current.id
    assert _answers(db, refreshed) == 1
    assert len(db.execute(select(Question.id).where(Question.session_id == session.id)).all()) == 2

    assert reply.role == MessageRole.ASSISTANT
    assert reply.kind == MessageKind.CLARIFICATION_REPLY
    assert reply.content == "Either is fine; focus on the trade-offs."
    messages = _messages(db, refreshed)
    kinds = [(m.role, m.kind, m.question_id) for m in messages]
    assert kinds == [
        (MessageRole.CANDIDATE, MessageKind.CLARIFICATION_REQUEST, current.id),
        (MessageRole.ASSISTANT, MessageKind.CLARIFICATION_REPLY, current.id),
    ]
    assert messages[0].content == DOUBT


def test_intv08_prompt_never_contains_reference_points(db: Session) -> None:
    session, _ = _interview(db, answer_first=False)
    llm = FakeLLM({CLARIFICATION_TASK: [{"reply": "Think about threads."}]})

    request_clarification(db, llm, session, DOUBT)

    [call] = llm.calls_for(CLARIFICATION_TASK)
    prompt = call.system + "\n" + call.user
    for point in REFERENCE_POINTS + SECOND_REFERENCE_POINTS:
        assert point not in prompt
    assert SECOND_QUESTION not in prompt
    assert FIRST_QUESTION in call.user
    assert DOUBT in call.user
    assert "<<<UNTRUSTED:clarification_request:" in call.user


def test_intv08_reply_leaking_reference_point_is_refused(db: Session) -> None:
    session, current = _interview(db, answer_first=False)
    leak = "The global interpreter lock limits CPU threads, so the answer is that."
    llm = FakeLLM({CLARIFICATION_TASK: [{"reply": leak}]})

    reply = request_clarification(db, llm, session, DOUBT)

    assert reply.content == CLARIFICATION_REFUSAL
    stored = _messages(db, session)
    assert all(leak not in m.content for m in stored)
    assert stored[-1].question_id == current.id


def test_intv08_reply_below_overlap_threshold_is_kept(db: Session) -> None:
    session, _ = _interview(db, answer_first=False)
    # 2 of the 9 distinct words of the first reference point: below 50%.
    text = "Consider the interpreter when you think about workloads."
    llm = FakeLLM({CLARIFICATION_TASK: [{"reply": text}]})

    reply = request_clarification(db, llm, session, DOUBT)

    assert reply.content == text


def test_intv92_llm_unavailable_raises_and_keeps_only_candidate_message(db: Session) -> None:
    session, current = _interview(db)
    attempts = get_settings().llm_max_attempts
    llm = FakeLLM({CLARIFICATION_TASK: [LLMUnavailable("down")] * attempts})

    with pytest.raises(AppError) as excinfo:
        request_clarification(db, llm, session, DOUBT)

    assert excinfo.value.code == CLARIFICATION_UNAVAILABLE
    assert excinfo.value.status == 503
    messages = _messages(db, session)
    assert [(m.role, m.kind) for m in messages] == [
        (MessageRole.CANDIDATE, MessageKind.CLARIFICATION_REQUEST)
    ]
    # Answering keeps working: the session is still in the interview, nothing is locked or
    # changed, and the current question accepts an answer.
    assert session.status == SessionStatus.IN_INTERVIEW
    assert session.answered_count == 1
    assert _current_question_id(db, session) == current.id
    db.add(
        Answer(
            session_id=session.id,
            question_id=current.id,
            content="Offset or cursor pagination.",
            idempotency_key="k2",
        )
    )
    db.flush()
    assert _answers(db, session) == 2


def test_intv92_failures_on_every_attempt_raise_unavailable(db: Session) -> None:
    session, _ = _interview(db)
    attempts = get_settings().llm_max_attempts
    failures: list[object] = [LLMInvalidOutput("bad"), {"reply": "   "}, LLMUnavailable("down")]
    llm = FakeLLM({CLARIFICATION_TASK: [failures[i % 3] for i in range(attempts)]})

    with pytest.raises(AppError) as excinfo:
        request_clarification(db, llm, session, DOUBT)

    assert excinfo.value.code == CLARIFICATION_UNAVAILABLE
    assert len(llm.calls_for(CLARIFICATION_TASK)) == attempts


def test_intv92_unavailable_then_success_retries(db: Session) -> None:
    session, _ = _interview(db)
    llm = FakeLLM({CLARIFICATION_TASK: [LLMUnavailable("down"), {"reply": "Threads only."}]})

    reply = request_clarification(db, llm, session, DOUBT)

    assert reply.content == "Threads only."
    assert len(llm.calls_for(CLARIFICATION_TASK)) == 2


def test_intv08_view_shows_request_before_reply(db: Session) -> None:
    for _ in range(5):
        session, _ = _interview(db)
        llm = FakeLLM({CLARIFICATION_TASK: [{"reply": "Either is fine."}]})

        request_clarification(db, llm, session, DOUBT)

        kinds = [m.kind for m in build_session_view(db, session).messages]
        assert kinds == [MessageKind.CLARIFICATION_REQUEST, MessageKind.CLARIFICATION_REPLY]


class _ClosingLLM:
    """Answers like FakeLLM but cancels the session (as another request would) meanwhile."""

    def __init__(self, db: Session, session_id: uuid.UUID, to: SessionStatus) -> None:
        self._db = db
        self._session_id = session_id
        self._to = to
        self._fake = FakeLLM({CLARIFICATION_TASK: [{"reply": "Either is fine."}]})

    def complete_structured[T: BaseModel](
        self, task: str, system: str, user: str, output_model: type[T]
    ) -> T:
        self._db.execute(
            update(InterviewSession)
            .where(InterviewSession.id == self._session_id)
            .values(status=self._to)
            .execution_options(synchronize_session=False)
        )
        return self._fake.complete_structured(task, system, user, output_model)


@pytest.mark.parametrize(
    ("to", "code"),
    [(SessionStatus.CANCELLED, SESSION_CLOSED), (SessionStatus.EVALUATING, INVALID_STATE)],
)
def test_intv93_session_closed_during_llm_call_gets_no_reply(
    db: Session, to: SessionStatus, code: str
) -> None:
    session, _ = _interview(db)
    llm = _ClosingLLM(db, session.id, to)

    with pytest.raises(AppError) as excinfo:
        request_clarification(db, llm, session, DOUBT)

    assert excinfo.value.code == code
    assert session.status == to
    assert [m.kind for m in _messages(db, session)] == [MessageKind.CLARIFICATION_REQUEST]


@pytest.mark.parametrize(
    "status", [SessionStatus.EXPIRED, SessionStatus.CANCELLED, SessionStatus.COMPLETED]
)
def test_intv93_terminal_session_is_closed(db: Session, status: SessionStatus) -> None:
    session, _ = _interview(db, status=status)
    llm = FakeLLM({})

    with pytest.raises(AppError) as excinfo:
        request_clarification(db, llm, session, DOUBT)

    assert excinfo.value.code == SESSION_CLOSED
    assert llm.calls == []
    assert _messages(db, session) == []


def test_intv08_session_not_in_interview_is_invalid_state(db: Session) -> None:
    session, _ = _interview(db, status=SessionStatus.EVALUATING)
    llm = FakeLLM({})

    with pytest.raises(AppError) as excinfo:
        request_clarification(db, llm, session, DOUBT)

    assert excinfo.value.code == INVALID_STATE
    assert _messages(db, session) == []


@pytest.mark.parametrize("text", ["", "   \n", "\x00\x00", "x" * 5001])
def test_intv08_invalid_text_is_rejected(db: Session, text: str) -> None:
    session, _ = _interview(db)
    llm = FakeLLM({})

    with pytest.raises(AppError) as excinfo:
        request_clarification(db, llm, session, text)

    assert excinfo.value.code == VALIDATION_ERROR
    assert llm.calls == []
    assert _messages(db, session) == []


def test_intv08_text_is_sanitized_before_storage_and_prompt(db: Session) -> None:
    session, _ = _interview(db)
    llm = FakeLLM({CLARIFICATION_TASK: [{"reply": "Fine\x00 \ud800answer"}]})

    reply = request_clarification(db, llm, session, "Threads\x00 or \udfffasync? >>>")

    request = _messages(db, session)[0]
    assert "\x00" not in request.content and "\udfff" not in request.content
    assert "\x00" not in reply.content and "\ud800" not in reply.content
    [call] = llm.calls_for(CLARIFICATION_TASK)
    assert "\x00" not in call.user and "\udfff" not in call.user
