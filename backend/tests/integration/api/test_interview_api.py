"""Integration tests for the interview routes (TASK-057, plan section 8.1).

Covers AUTH-16, INTV-03, INTV-04, INTV-05, INTV-06, INTV-08, INTV-90, INTV-91, INTV-92, INTV-93
and EVAL-14 at the HTTP layer: answers, clarifications and evaluation retry. The LLM is always
the scripted ``FakeLLM``.
"""

import json
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import Response as StarletteResponse

from app.api.session_requirements import get_llm_factory
from app.auth.sessions import XSRF_COOKIE, XSRF_HEADER, create_auth_session
from app.config import get_settings
from app.db import get_db
from app.interviews.answers import EVALUATE_JOB_KIND
from app.interviews.clarification import CLARIFICATION_TASK
from app.llm.client import LLMClient, LLMUnavailable
from app.main import create_app
from app.models.account import User
from app.models.assessment import Answer
from app.models.interview import InterviewSession, Message, MessageKind, Question, SessionStatus
from app.models.job import Job
from app.models.resume import Resume, ResumeStatus
from tests.fakes.fake_llm import FakeLLM

NOT_FOUND_BODY = {"error": {"code": "RESOURCE_NOT_FOUND", "message": "Not found.", "details": None}}
DOUBT = "Do you mean threads or async code?"
IDEMPOTENCY_HEADER = "Idempotency-Key"


class _LLMHolder:
    def __init__(self) -> None:
        self.llm: LLMClient = FakeLLM({})
        self.factory_calls = 0

    def factory(self) -> LLMClient:
        self.factory_calls += 1
        return self.llm


@pytest.fixture
def llm_holder() -> _LLMHolder:
    return _LLMHolder()


@pytest.fixture
def client(db: Session, llm_holder: _LLMHolder) -> Iterator[TestClient]:
    app = create_app()

    def override_db() -> Iterator[Session]:
        # Same contract as app.db.get_db: roll back what the request left uncommitted.
        try:
            yield db
        except Exception:
            db.rollback()
            raise

    def override_llm_factory() -> Callable[[], LLMClient]:
        return llm_holder.factory

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_llm_factory] = override_llm_factory
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _user(db: Session) -> User:
    settings = get_settings()
    user = User(
        email_normalized=f"user-{uuid.uuid4().hex}@example.com",
        email_verified_at=datetime.now(UTC),
        terms_version=settings.terms_version,
        privacy_version=settings.privacy_version,
        terms_accepted_at=datetime.now(UTC),
    )
    db.add(user)
    db.flush()
    return user


def _sign_in(client: TestClient, db: Session, user: User) -> None:
    response = StarletteResponse()
    create_auth_session(db, user, response)
    db.commit()
    client.cookies.clear()
    for header in response.headers.getlist("set-cookie"):
        cookie: SimpleCookie = SimpleCookie()
        cookie.load(header)
        for name, morsel in cookie.items():
            client.cookies.set(name, morsel.value)


def _interview(
    db: Session,
    user: User,
    status: SessionStatus = SessionStatus.IN_INTERVIEW,
    planned: int = 2,
) -> tuple[InterviewSession, list[Question]]:
    resume = Resume(user_id=user.id, filename="cv.pdf", status=ResumeStatus.READY, extraction=[])
    db.add(resume)
    db.flush()
    session = InterviewSession(
        user_id=user.id,
        resume_id=resume.id,
        resume_name=resume.filename,
        status=status,
        language="en",
        snapshot=[],
        planned_count=planned,
        answered_count=0,
        proposal={"planned_count": planned, "skills": ["Python"]},
    )
    db.add(session)
    db.flush()
    questions = [
        Question(
            session_id=session.id,
            position=position,
            skill_name="Python",
            text=f"Question number {position} about Python?",
            reference_points=[f"Secret reference point {position}"],
        )
        for position in range(1, planned + 1)
    ]
    db.add_all(questions)
    db.flush()
    db.commit()
    return session, questions


def _post(
    client: TestClient, path: str, body: Any = None, headers: dict[str, str] | None = None
) -> Response:
    content = json.dumps(body) if body is not None else None
    xsrf = client.cookies.get(XSRF_COOKIE)
    all_headers = {"content-type": "application/json", **(headers or {})}
    if xsrf:
        all_headers[XSRF_HEADER] = xsrf
    return client.post(path, content=content, headers=all_headers)


def _answer(
    client: TestClient,
    session_id: Any,
    question_id: Any,
    content: str = "I would use multiprocessing.",
    key: str | None = None,
) -> Response:
    headers = {IDEMPOTENCY_HEADER: key if key is not None else uuid.uuid4().hex}
    return _post(
        client,
        f"/api/sessions/{session_id}/answers",
        {"question_id": str(question_id), "content": content},
        headers,
    )


def _refreshed(db: Session, session_id: Any) -> InterviewSession:
    db.expire_all()
    session = db.get(InterviewSession, session_id)
    assert session is not None
    return session


def _answer_count(db: Session, session_id: Any) -> int:
    return db.execute(
        select(func.count()).select_from(Answer).where(Answer.session_id == session_id)
    ).scalar_one()


def _evaluate_jobs(db: Session, session_id: Any) -> int:
    jobs = db.execute(select(Job.payload).where(Job.kind == EVALUATE_JOB_KIND)).scalars().all()
    return sum(1 for payload in jobs if payload.get("session_id") == str(session_id))


def _routes(session_id: Any, question_id: Any) -> list[tuple[str, Any, dict[str, str]]]:
    return [
        (
            f"/api/sessions/{session_id}/answers",
            {"question_id": str(question_id), "content": "An answer."},
            {IDEMPOTENCY_HEADER: uuid.uuid4().hex},
        ),
        (f"/api/sessions/{session_id}/clarifications", {"text": DOUBT}, {}),
        (f"/api/sessions/{session_id}/evaluation/retry", None, {}),
    ]


# --- authentication and isolation (AUTH-16) ----------------------------------------------------


@pytest.mark.parametrize("index", range(3))
def test_auth_16_routes_require_authentication(client: TestClient, index: int) -> None:
    path, body, headers = _routes(uuid.uuid4(), uuid.uuid4())[index]
    assert _post(client, path, body, headers).status_code == 401


@pytest.mark.parametrize(
    ("index", "status"),
    [
        (0, SessionStatus.IN_INTERVIEW),
        (1, SessionStatus.IN_INTERVIEW),
        (2, SessionStatus.EVALUATION_FAILED),
    ],
)
def test_auth_16_other_users_session_gets_uniform_404(
    client: TestClient, db: Session, llm_holder: _LLMHolder, index: int, status: SessionStatus
) -> None:
    owner = _user(db)
    session, questions = _interview(db, owner, status)
    _sign_in(client, db, _user(db))
    llm_holder.llm = FakeLLM({CLARIFICATION_TASK: [{"reply": "Either works."}]})

    path, body, headers = _routes(session.id, questions[0].id)[index]
    response = _post(client, path, body, headers)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY
    assert llm_holder.factory_calls == 0
    refreshed = _refreshed(db, session.id)
    assert refreshed.status == status
    assert refreshed.answered_count == 0
    assert _answer_count(db, session.id) == 0


@pytest.mark.parametrize("session_id", ["not-a-uuid", str(uuid.uuid4())])
@pytest.mark.parametrize("index", range(3))
def test_auth_16_unknown_or_malformed_id_gets_same_404(
    client: TestClient, db: Session, session_id: str, index: int
) -> None:
    _sign_in(client, db, _user(db))
    path, body, headers = _routes(session_id, uuid.uuid4())[index]
    response = _post(client, path, body, headers)
    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY


# --- POST /answers (INTV-03, INTV-04, INTV-05, INTV-06, INTV-90, INTV-91, INTV-93) --------------


def test_intv_03_answer_moves_counter_and_shows_next_question(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    session, questions = _interview(db, user, planned=3)
    _sign_in(client, db, user)

    response = _answer(client, session.id, questions[0].id)

    assert response.status_code == 200
    body = response.json()
    assert body["counter"] == {"planned": 3, "answered": 1, "remaining": 2}
    assert body["current_question"]["id"] == str(questions[1].id)
    assert "reference_points" not in json.dumps(body)


def test_intv_06_same_idempotency_key_twice_counts_once(client: TestClient, db: Session) -> None:
    user = _user(db)
    session, questions = _interview(db, user, planned=3)
    _sign_in(client, db, user)
    key = uuid.uuid4().hex

    first = _answer(client, session.id, questions[0].id, key=key)
    second = _answer(client, session.id, questions[0].id, key=key)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["counter"]["answered"] == 1
    assert second.json()["counter"]["answered"] == 1
    assert _refreshed(db, session.id).answered_count == 1
    assert _answer_count(db, session.id) == 1


def test_intv_06_replay_of_last_answer_after_evaluation_started_returns_200(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    session, questions = _interview(db, user, planned=1)
    _sign_in(client, db, user)
    key = uuid.uuid4().hex

    first = _answer(client, session.id, questions[0].id, key=key)
    second = _answer(client, session.id, questions[0].id, key=key)

    assert first.status_code == 200
    assert first.json()["status"] == "evaluating"
    assert second.status_code == 200
    assert _refreshed(db, session.id).answered_count == 1
    assert _evaluate_jobs(db, session.id) == 1


def test_intv_06_missing_idempotency_key_returns_422(client: TestClient, db: Session) -> None:
    user = _user(db)
    session, questions = _interview(db, user)
    _sign_in(client, db, user)

    response = _post(
        client,
        f"/api/sessions/{session.id}/answers",
        {"question_id": str(questions[0].id), "content": "An answer."},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _refreshed(db, session.id).answered_count == 0


@pytest.mark.parametrize(
    "key",
    ["", " ", "a" * 65, "key with space", "kéy", "k\tey", "x" * 1000],
)
def test_intv_06_invalid_idempotency_key_returns_validation_error(
    client: TestClient, db: Session, key: str
) -> None:
    user = _user(db)
    session, questions = _interview(db, user)
    _sign_in(client, db, user)

    response = client.post(
        f"/api/sessions/{session.id}/answers",
        content=json.dumps({"question_id": str(questions[0].id), "content": "An answer."}),
        headers={
            "content-type": "application/json",
            XSRF_HEADER: client.cookies.get(XSRF_COOKIE) or "",
            # Raw bytes so non-ASCII (latin-1) values reach the server unchanged.
            IDEMPOTENCY_HEADER: key.encode("latin-1"),
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _refreshed(db, session.id).answered_count == 0


def test_intv_06_key_of_64_ascii_chars_is_accepted(client: TestClient, db: Session) -> None:
    user = _user(db)
    session, questions = _interview(db, user)
    _sign_in(client, db, user)

    response = _answer(client, session.id, questions[0].id, key="A-z_0.9:~" + "k" * 55)

    assert response.status_code == 200
    assert _refreshed(db, session.id).answered_count == 1


@pytest.mark.parametrize("content", ["", "   ", "\n\t ", "\x00\x00", "\ud800"])
def test_intv_04_blank_answer_is_rejected_without_counter_change(
    client: TestClient, db: Session, content: str
) -> None:
    user = _user(db)
    session, questions = _interview(db, user)
    _sign_in(client, db, user)

    response = _answer(client, session.id, questions[0].id, content=content)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EMPTY_ANSWER"
    assert _refreshed(db, session.id).answered_count == 0


def test_intv_05_answer_over_limit_is_rejected_with_limit(client: TestClient, db: Session) -> None:
    user = _user(db)
    session, questions = _interview(db, user)
    _sign_in(client, db, user)
    limit = get_settings().max_answer_chars

    response = _answer(client, session.id, questions[0].id, content="a" * (limit + 1))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ANSWER_TOO_LONG"
    assert response.json()["error"]["details"] == {"limit": limit}
    assert _refreshed(db, session.id).answered_count == 0


def test_intv_05_answer_at_limit_with_wide_chars_fits_body_cap(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    session, questions = _interview(db, user)
    _sign_in(client, db, user)
    limit = get_settings().max_answer_chars
    # Astral characters: 4 bytes in UTF-8, 12 bytes as JSON \u escapes (json.dumps default).
    content = "\U0001f600" * limit

    response = _answer(client, session.id, questions[0].id, content=content)

    assert response.status_code == 200
    assert _refreshed(db, session.id).answered_count == 1


def test_intv_05_oversized_body_is_rejected_before_parsing(client: TestClient, db: Session) -> None:
    user = _user(db)
    session, questions = _interview(db, user)
    _sign_in(client, db, user)

    response = _answer(client, session.id, questions[0].id, content="a" * 1_000_000)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _refreshed(db, session.id).answered_count == 0


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"content": "An answer."},
        {"question_id": "not-a-uuid", "content": "An answer."},
        {"question_id": None, "content": "An answer."},
        {"content": 42, "question_id": "00000000-0000-0000-0000-000000000000"},
        [],
    ],
)
def test_intv_04_malformed_answer_body_returns_validation_error(
    client: TestClient, db: Session, body: Any
) -> None:
    user = _user(db)
    session, _questions = _interview(db, user)
    _sign_in(client, db, user)

    response = _post(
        client, f"/api/sessions/{session.id}/answers", body, {IDEMPOTENCY_HEADER: "k-1"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_intv_90_second_answer_to_same_question_gets_409_with_session(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    session, questions = _interview(db, user, planned=3)
    _sign_in(client, db, user)

    first = _answer(client, session.id, questions[0].id, content="From tab A.")
    second = _answer(client, session.id, questions[0].id, content="From tab B.")

    assert first.status_code == 200
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "QUESTION_ALREADY_ANSWERED"
    assert error["details"]["session"]["counter"]["answered"] == 1
    assert error["details"]["session"]["current_question"]["id"] == str(questions[1].id)
    assert _refreshed(db, session.id).answered_count == 1


def test_intv_91_answer_to_non_current_question_gets_409_with_session(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    session, questions = _interview(db, user, planned=3)
    _sign_in(client, db, user)

    response = _answer(client, session.id, questions[2].id)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "NOT_CURRENT_QUESTION"
    assert error["details"]["session"]["id"] == str(session.id)
    assert error["details"]["session"]["current_question"]["id"] == str(questions[0].id)
    assert _refreshed(db, session.id).answered_count == 0


def test_intv_03_question_of_another_session_gets_404(client: TestClient, db: Session) -> None:
    user = _user(db)
    session, _questions = _interview(db, user)
    _other, other_questions = _interview(db, user, SessionStatus.CANCELLED)
    _sign_in(client, db, user)

    response = _answer(client, session.id, other_questions[0].id)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY


@pytest.mark.parametrize(
    "status", [SessionStatus.CANCELLED, SessionStatus.EXPIRED, SessionStatus.COMPLETED]
)
def test_intv_93_answer_to_closed_session_is_rejected(
    client: TestClient, db: Session, status: SessionStatus
) -> None:
    user = _user(db)
    session, questions = _interview(db, user, status)
    _sign_in(client, db, user)

    response = _answer(client, session.id, questions[0].id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SESSION_CLOSED"
    assert _answer_count(db, session.id) == 0


def test_intv_03_no_edit_routes_for_answers(client: TestClient, db: Session) -> None:
    user = _user(db)
    session, questions = _interview(db, user)
    _sign_in(client, db, user)
    _answer(client, session.id, questions[0].id)

    path = f"/api/sessions/{session.id}/answers"
    xsrf = {XSRF_HEADER: client.cookies.get(XSRF_COOKIE) or ""}
    assert client.put(path, json={}, headers=xsrf).status_code == 405
    assert client.patch(path, json={}, headers=xsrf).status_code == 405


# --- POST /clarifications (INTV-08, INTV-92, INTV-93) ------------------------------------------


def test_intv_08_clarification_returns_message_without_counter_change(
    client: TestClient, db: Session, llm_holder: _LLMHolder
) -> None:
    user = _user(db)
    session, questions = _interview(db, user)
    _sign_in(client, db, user)
    llm_holder.llm = FakeLLM(
        {CLARIFICATION_TASK: [{"reply": "Either works; focus on trade-offs."}]}
    )

    response = _post(client, f"/api/sessions/{session.id}/clarifications", {"text": DOUBT})

    assert response.status_code == 200
    message = response.json()["message"]
    assert message["role"] == "assistant"
    assert message["kind"] == "clarification_reply"
    assert message["content"] == "Either works; focus on trade-offs."
    refreshed = _refreshed(db, session.id)
    assert refreshed.answered_count == 0
    kinds = db.execute(select(Message.kind).where(Message.session_id == session.id)).scalars()
    assert sorted(kinds) == sorted(
        [MessageKind.CLARIFICATION_REQUEST, MessageKind.CLARIFICATION_REPLY]
    )
    view = client.get(f"/api/sessions/{session.id}").json()
    assert view["current_question"]["id"] == str(questions[0].id)


def test_intv_92_clarification_with_llm_unavailable_returns_503_and_answers_still_work(
    client: TestClient, db: Session, llm_holder: _LLMHolder
) -> None:
    user = _user(db)
    session, questions = _interview(db, user)
    _sign_in(client, db, user)
    attempts = get_settings().llm_max_attempts
    llm_holder.llm = FakeLLM({CLARIFICATION_TASK: [LLMUnavailable] * attempts})

    response = _post(client, f"/api/sessions/{session.id}/clarifications", {"text": DOUBT})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CLARIFICATION_UNAVAILABLE"
    assert _refreshed(db, session.id).answered_count == 0
    assert _answer(client, session.id, questions[0].id).status_code == 200
    assert _refreshed(db, session.id).answered_count == 1


def test_intv_92_llm_client_misconfiguration_returns_503(
    client: TestClient, db: Session, llm_holder: _LLMHolder
) -> None:
    user = _user(db)
    session, _questions = _interview(db, user)
    _sign_in(client, db, user)

    def broken() -> LLMClient:
        raise ValueError("host not allowed")

    llm_holder.factory = broken  # type: ignore[method-assign]

    response = _post(client, f"/api/sessions/{session.id}/clarifications", {"text": DOUBT})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CLARIFICATION_UNAVAILABLE"


@pytest.mark.parametrize(
    "status", [SessionStatus.CANCELLED, SessionStatus.EXPIRED, SessionStatus.COMPLETED]
)
def test_intv_93_clarification_to_closed_session_is_rejected(
    client: TestClient, db: Session, llm_holder: _LLMHolder, status: SessionStatus
) -> None:
    user = _user(db)
    session, _questions = _interview(db, user, status)
    _sign_in(client, db, user)
    llm_holder.llm = FakeLLM({CLARIFICATION_TASK: [{"reply": "Either works."}]})

    response = _post(client, f"/api/sessions/{session.id}/clarifications", {"text": DOUBT})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SESSION_CLOSED"
    assert llm_holder.llm.remaining(CLARIFICATION_TASK) == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "body", [{}, {"text": None}, {"text": 42}, {"text": "x", "extra": 1}, {"text": "\x00 "}, []]
)
def test_intv_08_malformed_clarification_returns_validation_error(
    client: TestClient, db: Session, body: Any
) -> None:
    user = _user(db)
    session, _questions = _interview(db, user)
    _sign_in(client, db, user)

    response = _post(client, f"/api/sessions/{session.id}/clarifications", body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --- POST /evaluation/retry (EVAL-14) -----------------------------------------------------------


def test_eval_14_retry_moves_failed_session_to_evaluating(client: TestClient, db: Session) -> None:
    user = _user(db)
    session, _questions = _interview(db, user, SessionStatus.EVALUATION_FAILED)
    _sign_in(client, db, user)

    response = _post(client, f"/api/sessions/{session.id}/evaluation/retry")

    assert response.status_code == 200
    assert response.json()["status"] == "evaluating"
    assert _refreshed(db, session.id).status == SessionStatus.EVALUATING
    assert _evaluate_jobs(db, session.id) == 1


@pytest.mark.parametrize(
    "status", [SessionStatus.IN_INTERVIEW, SessionStatus.EVALUATING, SessionStatus.COMPLETED]
)
def test_eval_14_retry_from_other_state_returns_409(
    client: TestClient, db: Session, status: SessionStatus
) -> None:
    user = _user(db)
    session, _questions = _interview(db, user, status)
    _sign_in(client, db, user)

    response = _post(client, f"/api/sessions/{session.id}/evaluation/retry")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"
    assert _refreshed(db, session.id).status == status
    assert _evaluate_jobs(db, session.id) == 0
