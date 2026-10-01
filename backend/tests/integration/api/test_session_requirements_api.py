"""Integration tests for the requirements and plan routes (TASK-056, plan section 8.1).

Covers AUTH-16, PLAN-03, PLAN-04, PLAN-05, PLAN-07, PLAN-08, PLAN-09, PLAN-10, PLAN-15,
PLAN-90 and PLAN-92 at the HTTP layer. The LLM is always the scripted ``FakeLLM``.
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
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import Response as StarletteResponse

from app.api.session_requirements import MAX_REQUIREMENTS_BODY_BYTES, get_llm_factory
from app.auth.sessions import XSRF_COOKIE, XSRF_HEADER, create_auth_session
from app.config import get_settings
from app.db import get_db
from app.interviews.requirement_list import PREPARE_QUESTIONS_JOB
from app.interviews.requirements import (
    MAX_REQUIREMENTS_CHARS,
    NOT_ENGLISH_MESSAGE,
    REQUIREMENTS_TASK,
)
from app.llm.client import LLMClient, LLMUnavailable
from app.main import create_app
from app.models.account import User
from app.models.interview import InterviewSession, SessionStatus
from app.models.job import Job
from app.models.resume import Resume, ResumeStatus
from tests.fakes.fake_llm import FakeLLM

NOT_FOUND_BODY = {"error": {"code": "RESOURCE_NOT_FOUND", "message": "Not found.", "details": None}}

ENGLISH_TEXT = (
    "We are hiring a backend engineer to build and operate our payment platform.\n"
    "Required: strong experience with Python and PostgreSQL in production.\n"
    "Nice to have: knowledge of Kafka and observability tools.\n"
    "Good communication skills and 5+ years of professional experience are expected."
)
PORTUGUESE_TEXT = (
    "Estamos contratando uma pessoa desenvolvedora backend para construir nossa plataforma.\n"
    "Requisitos: experiência sólida com Python e bancos de dados relacionais, conhecimento de "
    "contêineres e orquestração em produção, boa comunicação e trabalho em equipe."
)


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


def _xsrf(client: TestClient) -> dict[str, str]:
    value = client.cookies.get(XSRF_COOKIE)
    return {XSRF_HEADER: value} if value else {}


def _session(
    db: Session,
    user: User,
    status: SessionStatus = SessionStatus.COLLECTING_REQUIREMENTS,
    **fields: Any,
) -> InterviewSession:
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
        **fields,
    )
    db.add(session)
    db.flush()
    db.commit()
    return session


def _item(
    item_id: str,
    name: str,
    classification: str = "required",
    pending: bool = False,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "original_terms": [name],
        "classification": classification,
        "level": None,
        "pending_clarification": pending,
        "clarification_question": "Is it required?" if pending else None,
    }


def _awaiting(db: Session, user: User, items: list[dict[str, Any]], **fields: Any) -> Any:
    return _session(
        db,
        user,
        SessionStatus.AWAITING_CONFIRMATION,
        requirements_text=ENGLISH_TEXT,
        requirement_items=items,
        non_technical=[],
        **fields,
    )


def _send(client: TestClient, method: str, path: str, body: Any = None) -> Response:
    content = json.dumps(body) if body is not None else None
    headers = {"content-type": "application/json", **_xsrf(client)}
    return client.request(method, path, content=content, headers=headers)


def _llm_answer(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items, "non_technical": ["Good communication"]}


def _llm_item(name: str, classification: str = "required", ambiguous: bool = False) -> Any:
    return {
        "name": name,
        "original_terms": [name],
        "classification": classification,
        "level": None,
        "ambiguous": ambiguous,
        "clarification_question": f"Is {name} required?" if ambiguous else None,
    }


def _routes(session_id: Any) -> list[tuple[str, str, Any]]:
    return [
        ("POST", f"/api/sessions/{session_id}/requirements", {"text": ENGLISH_TEXT}),
        ("PUT", f"/api/sessions/{session_id}/requirement-list", {"items": []}),
        ("POST", f"/api/sessions/{session_id}/requirement-list/confirm", None),
        ("POST", f"/api/sessions/{session_id}/plan/confirm", None),
        ("POST", f"/api/sessions/{session_id}/preparation/retry", None),
    ]


def _status(db: Session, session_id: Any) -> SessionStatus:
    db.expire_all()
    return db.execute(
        select(InterviewSession.status).where(InterviewSession.id == session_id)
    ).scalar_one()


# --- authentication and isolation (AUTH-16) ----------------------------------------------------


@pytest.mark.parametrize("index", range(5))
def test_auth_16_routes_require_authentication(client: TestClient, index: int) -> None:
    method, path, body = _routes(uuid.uuid4())[index]
    response = _send(client, method, path, body)
    assert response.status_code == 401


@pytest.mark.parametrize("index", range(5))
def test_auth_16_other_users_session_gets_uniform_404(
    client: TestClient, db: Session, llm_holder: _LLMHolder, index: int
) -> None:
    owner = _user(db)
    session = _awaiting(db, owner, [_item("a", "Python")], proposal=None)
    intruder = _user(db)
    _sign_in(client, db, intruder)
    llm_holder.llm = FakeLLM({REQUIREMENTS_TASK: [_llm_answer([_llm_item("Java")])]})

    method, path, body = _routes(session.id)[index]
    response = _send(client, method, path, body)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY
    assert llm_holder.factory_calls == 0
    assert _status(db, session.id) == SessionStatus.AWAITING_CONFIRMATION


@pytest.mark.parametrize("session_id", ["not-a-uuid", str(uuid.uuid4())])
@pytest.mark.parametrize("index", range(5))
def test_auth_16_unknown_or_malformed_id_gets_same_404(
    client: TestClient, db: Session, session_id: str, index: int
) -> None:
    _sign_in(client, db, _user(db))
    method, path, body = _routes(session_id)[index]
    response = _send(client, method, path, body)
    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY


# --- POST /requirements (PLAN-03, PLAN-04, PLAN-15, PLAN-90) ------------------------------------


def test_plan_03_requirements_are_structured_into_session_view(
    client: TestClient, db: Session, llm_holder: _LLMHolder
) -> None:
    user = _user(db)
    session = _session(db, user)
    _sign_in(client, db, user)
    llm_holder.llm = FakeLLM(
        {
            REQUIREMENTS_TASK: [
                _llm_answer(
                    [
                        _llm_item("Python"),
                        _llm_item("Kafka", "nice_to_have"),
                        _llm_item("Cloud", ambiguous=True),
                    ]
                )
            ]
        }
    )

    response = _send(
        client, "POST", f"/api/sessions/{session.id}/requirements", {"text": ENGLISH_TEXT}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_confirmation"
    items = {item["name"]: item for item in body["requirements"]["items"]}
    assert items["Python"]["classification"] == "required"
    assert items["Kafka"]["classification"] == "nice_to_have"
    assert items["Cloud"]["pending_clarification"] is True
    assert _status(db, session.id) == SessionStatus.AWAITING_CONFIRMATION


@pytest.mark.parametrize("text", ["", "   ", " \n\t ", "\x00\x00"])
def test_plan_90_blank_requirements_return_422_without_state_change(
    client: TestClient, db: Session, text: str
) -> None:
    user = _user(db)
    session = _session(db, user)
    _sign_in(client, db, user)

    response = _send(client, "POST", f"/api/sessions/{session.id}/requirements", {"text": text})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EMPTY_REQUIREMENTS"
    assert _status(db, session.id) == SessionStatus.COLLECTING_REQUIREMENTS


@pytest.mark.parametrize(
    "body", [{}, {"text": None}, {"text": 42}, {"text": ["a"]}, {"text": "x", "extra": 1}, []]
)
def test_plan_90_malformed_body_returns_validation_error(
    client: TestClient, db: Session, body: Any
) -> None:
    user = _user(db)
    session = _session(db, user)
    _sign_in(client, db, user)

    response = _send(client, "POST", f"/api/sessions/{session.id}/requirements", body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _status(db, session.id) == SessionStatus.COLLECTING_REQUIREMENTS


def test_plan_15_non_english_requirements_ask_for_english(
    client: TestClient, db: Session, llm_holder: _LLMHolder
) -> None:
    user = _user(db)
    session = _session(db, user)
    _sign_in(client, db, user)
    fake = FakeLLM({})
    llm_holder.llm = fake

    response = _send(
        client, "POST", f"/api/sessions/{session.id}/requirements", {"text": PORTUGUESE_TEXT}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "collecting_requirements"
    assert body["messages"][-1]["content"] == NOT_ENGLISH_MESSAGE
    assert fake.calls == []


def test_plan_03_llm_unavailable_returns_503_without_state_change(
    client: TestClient, db: Session, llm_holder: _LLMHolder
) -> None:
    user = _user(db)
    session = _session(db, user)
    _sign_in(client, db, user)
    attempts = get_settings().llm_max_attempts
    llm_holder.llm = FakeLLM({REQUIREMENTS_TASK: [LLMUnavailable] * attempts})

    response = _send(
        client, "POST", f"/api/sessions/{session.id}/requirements", {"text": ENGLISH_TEXT}
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "LLM_UNAVAILABLE"
    assert _status(db, session.id) == SessionStatus.COLLECTING_REQUIREMENTS


def test_plan_03_hostile_text_never_returns_500(
    client: TestClient, db: Session, llm_holder: _LLMHolder
) -> None:
    user = _user(db)
    session = _session(db, user)
    _sign_in(client, db, user)
    llm_holder.llm = FakeLLM({REQUIREMENTS_TASK: [_llm_answer([_llm_item("Python")])]})
    text = ENGLISH_TEXT + "\x00𐏿\x07 Python"

    response = _send(client, "POST", f"/api/sessions/{session.id}/requirements", {"text": text})

    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_confirmation"


def test_plan_03_full_length_requirements_fit_the_body_limit(
    client: TestClient, db: Session, llm_holder: _LLMHolder
) -> None:
    user = _user(db)
    session = _session(db, user)
    _sign_in(client, db, user)
    llm_holder.llm = FakeLLM({REQUIREMENTS_TASK: [_llm_answer([_llm_item("Python")])]})
    sentence = "Required: strong experience with Python and PostgreSQL in production. "
    text = (sentence * (MAX_REQUIREMENTS_CHARS // len(sentence) + 1))[:MAX_REQUIREMENTS_CHARS]

    response = _send(client, "POST", f"/api/sessions/{session.id}/requirements", {"text": text})

    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_confirmation"


def test_plan_03_oversized_body_is_rejected(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _session(db, user)
    _sign_in(client, db, user)
    text = "a" * (MAX_REQUIREMENTS_BODY_BYTES + 1)

    response = _send(client, "POST", f"/api/sessions/{session.id}/requirements", {"text": text})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _status(db, session.id) == SessionStatus.COLLECTING_REQUIREMENTS


def test_plan_03_requirements_in_wrong_state_return_409(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _session(db, user, SessionStatus.PREPARING_QUESTIONS, planned_count=1)
    _sign_in(client, db, user)

    response = _send(
        client, "POST", f"/api/sessions/{session.id}/requirements", {"text": ENGLISH_TEXT}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"


# --- PUT /requirement-list (PLAN-05) ------------------------------------------------------------


def test_plan_05_edited_list_is_reflected(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _awaiting(db, user, [_item("a", "Python"), _item("b", "Kafka", "nice_to_have")])
    _sign_in(client, db, user)
    items = [
        {"id": "a", "name": "Python", "classification": "nice_to_have", "level": "senior"},
        {"name": "Go", "classification": "required"},
    ]

    response = _send(
        client, "PUT", f"/api/sessions/{session.id}/requirement-list", {"items": items}
    )

    assert response.status_code == 200
    listed = response.json()["requirements"]["items"]
    assert [(i["name"], i["classification"], i["level"]) for i in listed] == [
        ("Python", "nice_to_have", "senior"),
        ("Go", "required", None),
    ]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"items": None},
        {"items": "x"},
        {"items": [{"name": "Go"}]},
        {"items": [{"name": "Go", "classification": "mandatory"}]},
        {"items": [{"name": 1, "classification": "required"}]},
        {"items": [{"name": "\x00 ", "classification": "required"}]},
        {"items": [], "extra": True},
    ],
)
def test_plan_05_malformed_list_returns_validation_error(
    client: TestClient, db: Session, body: Any
) -> None:
    user = _user(db)
    session = _awaiting(db, user, [_item("a", "Python")])
    _sign_in(client, db, user)

    response = _send(client, "PUT", f"/api/sessions/{session.id}/requirement-list", body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_plan_05_hostile_names_are_sanitized(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _awaiting(db, user, [_item("a", "Python")])
    _sign_in(client, db, user)
    items = [{"name": "Go\x00\ud800lang", "classification": "required"}]

    response = _send(
        client, "PUT", f"/api/sessions/{session.id}/requirement-list", {"items": items}
    )

    assert response.status_code == 200
    assert response.json()["requirements"]["items"][0]["name"] == "Go lang"


def test_plan_05_list_cannot_be_edited_after_plan_confirmed(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    session = _session(db, user, SessionStatus.PREPARING_QUESTIONS, planned_count=1)
    _sign_in(client, db, user)

    response = _send(client, "PUT", f"/api/sessions/{session.id}/requirement-list", {"items": []})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"


# --- POST /requirement-list/confirm (PLAN-04, PLAN-07, PLAN-08, PLAN-09) ------------------------


def test_plan_09_confirm_list_returns_proposal(client: TestClient, db: Session) -> None:
    user = _user(db)
    items = [_item("a", "Python"), _item("b", "Go"), _item("c", "Kafka", "nice_to_have")]
    session = _awaiting(db, user, items)
    _sign_in(client, db, user)

    response = _send(client, "POST", f"/api/sessions/{session.id}/requirement-list/confirm")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_confirmation"
    assert body["proposal"] == {"planned_count": 2, "skills": ["Python", "Go"]}


def test_plan_08_too_many_required_skills_returns_excess(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _awaiting(db, user, [_item(f"i{n}", f"Skill {n}") for n in range(21)])
    _sign_in(client, db, user)

    response = _send(client, "POST", f"/api/sessions/{session.id}/requirement-list/confirm")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "TOO_MANY_REQUIRED_SKILLS"
    assert error["details"] == {"count": 21, "excess": 1}
    assert "21" in error["message"]


def test_plan_07_no_required_skills_blocks_confirmation(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _awaiting(db, user, [_item("a", "Kafka", "nice_to_have")])
    _sign_in(client, db, user)

    response = _send(client, "POST", f"/api/sessions/{session.id}/requirement-list/confirm")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NO_REQUIRED_SKILLS"


def test_plan_04_pending_item_blocks_confirmation(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _awaiting(db, user, [_item("a", "Python"), _item("b", "Cloud", pending=True)])
    _sign_in(client, db, user)

    response = _send(client, "POST", f"/api/sessions/{session.id}/requirement-list/confirm")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PENDING_CLARIFICATION"


# --- POST /plan/confirm (PLAN-10) ---------------------------------------------------------------


def test_plan_10_confirm_plan_moves_to_preparing_questions(client: TestClient, db: Session) -> None:
    user = _user(db)
    names = ["Python", "Go", "PostgreSQL"]
    session = _awaiting(db, user, [_item(f"i{n}", name) for n, name in enumerate(names)])
    _sign_in(client, db, user)
    confirmed = _send(client, "POST", f"/api/sessions/{session.id}/requirement-list/confirm")
    assert confirmed.status_code == 200

    response = _send(client, "POST", f"/api/sessions/{session.id}/plan/confirm")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "preparing_questions"
    assert body["counter"]["planned"] == len(names)
    jobs = db.execute(select(Job).where(Job.kind == PREPARE_QUESTIONS_JOB)).scalars().all()
    assert [job.payload for job in jobs] == [{"session_id": str(session.id)}]


def test_plan_10_confirm_plan_without_proposal_returns_409(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _awaiting(db, user, [_item("a", "Python")])
    _sign_in(client, db, user)

    response = _send(client, "POST", f"/api/sessions/{session.id}/plan/confirm")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"
    assert _status(db, session.id) == SessionStatus.AWAITING_CONFIRMATION


# --- POST /preparation/retry (PLAN-92) ----------------------------------------------------------


def test_plan_92_retry_preparation_requeues(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _awaiting(db, user, [_item("a", "Python")])
    session.status = SessionStatus.PREPARATION_FAILED
    session.planned_count = 1
    db.commit()
    _sign_in(client, db, user)

    response = _send(client, "POST", f"/api/sessions/{session.id}/preparation/retry")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "preparing_questions"
    assert body["requirements"]["items"][0]["name"] == "Python"
    jobs = db.execute(select(Job).where(Job.kind == PREPARE_QUESTIONS_JOB)).scalars().all()
    assert len(jobs) == 1


def test_plan_92_retry_from_other_state_returns_409(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _awaiting(db, user, [_item("a", "Python")])
    _sign_in(client, db, user)

    response = _send(client, "POST", f"/api/sessions/{session.id}/preparation/retry")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"
