"""Integration tests for the interview session routes (TASK-055, plan section 8.1 "Sessions").

Covers AUTH-16, PLAN-01, PLAN-02, INTV-10, INTV-12, DATA-01, DATA-92 and LANG-01 at the HTTP
layer.
"""

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session
from starlette.responses import Response as StarletteResponse

from app.auth.sessions import XSRF_COOKIE, XSRF_HEADER, create_auth_session
from app.config import get_settings
from app.db import get_db
from app.interviews.sessions import FIRST_ASSISTANT_MESSAGE
from app.main import create_app
from app.models.account import User
from app.models.interview import InterviewSession, SessionStatus
from app.models.resume import Resume, ResumeStatus

NOT_FOUND_BODY = {"error": {"code": "RESOURCE_NOT_FOUND", "message": "Not found.", "details": None}}
SESSION_VIEW_KEYS = {
    "id",
    "status",
    "created_at",
    "language",
    "interview_level",
    "resume_name",
    "messages",
    "requirements",
    "proposal",
    "counter",
    "current_question",
    "answered",
    "report_available",
}


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    app = create_app()

    def override_db() -> Iterator[Session]:
        # Same contract as app.db.get_db: roll back what the request left uncommitted.
        try:
            yield db
        except Exception:
            db.rollback()
            raise

    app.dependency_overrides[get_db] = override_db
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


def _resume(
    db: Session, user: User, status: ResumeStatus = ResumeStatus.READY, filename: str = "cv.pdf"
) -> Resume:
    resume = Resume(user_id=user.id, filename=filename, status=status, extraction=[])
    db.add(resume)
    db.flush()
    return resume


def _session(
    db: Session,
    user: User,
    status: SessionStatus = SessionStatus.COLLECTING_REQUIREMENTS,
    **fields: Any,
) -> InterviewSession:
    resume = _resume(db, user)
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
    return session


def _post_json(client: TestClient, path: str, body: Any) -> Response:
    # json.dumps escapes lone surrogates, so hostile strings reach the server as JSON.
    return client.post(
        path,
        content=json.dumps(body),
        headers={"content-type": "application/json", **_xsrf(client)},
    )


def _start(client: TestClient, resume_id: Any, **extra: Any) -> Response:
    body: dict[str, Any] = {"resume_id": str(resume_id), "language": "en", **extra}
    return _post_json(client, "/api/sessions", body)


def _required_item(item_id: str, name: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "original_terms": [name],
        "classification": "required",
    }


# --- authentication --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/interview-options"),
        ("GET", "/api/sessions"),
        ("POST", "/api/sessions"),
        ("GET", f"/api/sessions/{uuid.uuid4()}"),
        ("POST", f"/api/sessions/{uuid.uuid4()}/cancel"),
    ],
)
def test_routes_require_authentication(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_post_routes_require_csrf_header(client: TestClient, db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user)
    session = _session(db, user)
    _sign_in(client, db, user)

    start = client.post("/api/sessions", json={"resume_id": str(resume.id), "language": "en"})
    cancel = client.post(f"/api/sessions/{session.id}/cancel")

    assert start.status_code == 403
    assert cancel.status_code == 403
    db.refresh(session)
    assert session.status == SessionStatus.COLLECTING_REQUIREMENTS


# --- interview options (LANG-01) -------------------------------------------------------------


def test_interview_options_lists_english_and_four_levels(client: TestClient, db: Session) -> None:
    _sign_in(client, db, _user(db))

    response = client.get("/api/interview-options")

    assert response.status_code == 200
    assert response.json() == {
        "languages": [{"code": "en", "label": "English"}],
        "levels": ["junior", "mid-level", "senior", "expert"],
    }


# --- start (PLAN-01, PLAN-02, LANG-01) -------------------------------------------------------


def test_start_creates_collecting_session_with_first_message(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    resume = _resume(db, user, filename="backend.pdf")
    _sign_in(client, db, user)

    response = _start(client, resume.id, interview_level="senior")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == SESSION_VIEW_KEYS
    assert body["status"] == "collecting_requirements"
    assert body["language"] == "en"
    assert body["interview_level"] == "senior"
    assert body["resume_name"] == "backend.pdf"
    assert [m["content"] for m in body["messages"]] == [FIRST_ASSISTANT_MESSAGE]
    stored = db.get(InterviewSession, uuid.UUID(body["id"]))
    assert stored is not None and stored.user_id == user.id


def test_start_without_level_defaults_to_none(client: TestClient, db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user)
    _sign_in(client, db, user)

    response = _start(client, resume.id)

    assert response.status_code == 201
    assert response.json()["interview_level"] is None


def test_start_with_open_session_returns_409_with_session_id(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    open_session = _session(db, user, SessionStatus.IN_INTERVIEW)
    resume = _resume(db, user)
    _sign_in(client, db, user)

    response = _start(client, resume.id)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "SESSION_IN_PROGRESS"
    assert error["details"] == {"session_id": str(open_session.id)}


@pytest.mark.parametrize("language", ["pt", "EN", "", "e\x00n", "\ud800"])
def test_start_with_unsupported_language_returns_422(
    client: TestClient, db: Session, language: str
) -> None:
    user = _user(db)
    resume = _resume(db, user)
    _sign_in(client, db, user)

    response = _post_json(
        client, "/api/sessions", {"resume_id": str(resume.id), "language": language}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "LANGUAGE_NOT_SUPPORTED"


def test_start_with_resume_not_ready_returns_409(client: TestClient, db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user, status=ResumeStatus.PROCESSING)
    _sign_in(client, db, user)

    response = _start(client, resume.id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RESUME_NOT_READY"


def test_start_with_resume_of_another_user_returns_404(client: TestClient, db: Session) -> None:
    owner = _user(db)
    resume = _resume(db, owner)
    _sign_in(client, db, _user(db))

    response = _start(client, resume.id)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY


@pytest.mark.parametrize(
    "body",
    [
        {"resume_id": "not-a-uuid", "language": "en"},
        {"resume_id": 123, "language": "en"},
        {"language": "en"},
        {"resume_id": str(uuid.uuid4())},
        {"resume_id": str(uuid.uuid4()), "language": 1},
        {"resume_id": str(uuid.uuid4()), "language": "en", "interview_level": "guru"},
        {"resume_id": str(uuid.uuid4()), "language": "en", "interview_level": "\x00"},
        {"resume_id": str(uuid.uuid4()), "language": "en", "user_id": str(uuid.uuid4())},
        ["not", "an", "object"],
        "\ud800",
    ],
)
def test_start_with_invalid_body_returns_422(client: TestClient, db: Session, body: Any) -> None:
    _sign_in(client, db, _user(db))

    response = _post_json(client, "/api/sessions", body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_start_with_oversized_body_is_rejected(client: TestClient, db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user)
    _sign_in(client, db, user)

    response = _post_json(
        client,
        "/api/sessions",
        {"resume_id": str(resume.id), "language": "en", "pad": "x" * 20_000},
    )

    assert response.status_code == 422
    assert db.query(InterviewSession).filter_by(user_id=user.id).count() == 0


# --- history (DATA-01, DATA-92) --------------------------------------------------------------


def test_list_without_sessions_returns_empty_list(client: TestClient, db: Session) -> None:
    _sign_in(client, db, _user(db))

    response = client.get("/api/sessions")

    assert response.status_code == 200
    assert response.json() == []


def test_list_returns_only_own_sessions_in_every_state(client: TestClient, db: Session) -> None:
    user = _user(db)
    other = _user(db)
    cancelled = _session(db, user, SessionStatus.CANCELLED)
    expired = _session(db, user, SessionStatus.EXPIRED)
    _session(db, other)
    _sign_in(client, db, user)

    response = client.get("/api/sessions")

    assert response.status_code == 200
    body = response.json()
    assert {entry["id"] for entry in body} == {str(cancelled.id), str(expired.id)}
    assert {entry["status"] for entry in body} == {"cancelled", "expired"}
    for entry in body:
        assert set(entry) == {
            "id",
            "created_at",
            "status",
            "resume_name",
            "required_skills",
            "completed_at",
        }
        assert entry["resume_name"] == "cv.pdf"


def test_list_shows_confirmed_skills_only(client: TestClient, db: Session) -> None:
    user = _user(db)
    draft = _session(
        db,
        user,
        SessionStatus.CANCELLED,
        requirement_items=[_required_item("r1", "Go")],
    )
    confirmed = _session(
        db,
        user,
        SessionStatus.COMPLETED,
        requirement_items=[_required_item("r1", "Python"), _required_item("r2", "SQL")],
        proposal={"planned_count": 2, "skills": ["Python", "SQL"]},
        planned_count=2,
        completed_at=datetime.now(UTC),
    )
    frozen = _session(
        db,
        user,
        SessionStatus.EXPIRED,
        requirement_items=[
            _required_item("r1", "Rust"),
            {**_required_item("r2", "Docker"), "classification": "nice_to_have"},
        ],
        planned_count=1,
    )
    _sign_in(client, db, user)

    body = {entry["id"]: entry for entry in client.get("/api/sessions").json()}

    assert body[str(draft.id)]["required_skills"] == []
    assert body[str(confirmed.id)]["required_skills"] == ["Python", "SQL"]
    assert body[str(confirmed.id)]["completed_at"] is not None
    assert body[str(frozen.id)]["required_skills"] == ["Rust"]
    assert body[str(frozen.id)]["completed_at"] is None


# --- detail (AUTH-16, INTV-10) ---------------------------------------------------------------


def test_get_returns_session_view_and_touches_active_session(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    old = datetime.now(UTC) - timedelta(days=10)
    session = _session(db, user, last_activity_at=old)
    _sign_in(client, db, user)

    response = client.get(f"/api/sessions/{session.id}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == SESSION_VIEW_KEYS
    assert body["id"] == str(session.id)
    db.refresh(session)
    assert session.last_activity_at > old


def test_get_terminal_session_does_not_touch_activity(client: TestClient, db: Session) -> None:
    user = _user(db)
    old = datetime.now(UTC) - timedelta(days=10)
    session = _session(db, user, SessionStatus.CANCELLED, last_activity_at=old)
    _sign_in(client, db, user)

    response = client.get(f"/api/sessions/{session.id}")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    db.refresh(session)
    assert session.last_activity_at == old


def test_get_and_cancel_of_another_user_return_404(client: TestClient, db: Session) -> None:
    owner = _user(db)
    session = _session(db, owner)
    _sign_in(client, db, _user(db))

    detail = client.get(f"/api/sessions/{session.id}")
    cancel = client.post(f"/api/sessions/{session.id}/cancel", headers=_xsrf(client))

    assert detail.status_code == 404
    assert detail.json() == NOT_FOUND_BODY
    assert cancel.status_code == 404
    assert cancel.json() == NOT_FOUND_BODY
    db.refresh(session)
    assert session.status == SessionStatus.COLLECTING_REQUIREMENTS


@pytest.mark.parametrize("session_id", [str(uuid.uuid4()), "not-a-uuid", "%00", "123"])
def test_unknown_or_malformed_id_returns_uniform_404(
    client: TestClient, db: Session, session_id: str
) -> None:
    _sign_in(client, db, _user(db))

    detail = client.get(f"/api/sessions/{session_id}")
    cancel = client.post(f"/api/sessions/{session_id}/cancel", headers=_xsrf(client))

    assert detail.status_code == 404
    assert detail.json() == NOT_FOUND_BODY
    assert cancel.status_code == 404
    assert cancel.json() == NOT_FOUND_BODY


# --- cancel (INTV-12) ------------------------------------------------------------------------


def test_cancel_moves_open_session_to_cancelled(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _session(db, user, SessionStatus.IN_INTERVIEW)
    _sign_in(client, db, user)

    response = client.post(f"/api/sessions/{session.id}/cancel", headers=_xsrf(client))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == SESSION_VIEW_KEYS
    assert body["status"] == "cancelled"
    assert body["report_available"] is False
    db.refresh(session)
    assert session.status == SessionStatus.CANCELLED


@pytest.mark.parametrize(
    "status", [SessionStatus.CANCELLED, SessionStatus.EXPIRED, SessionStatus.COMPLETED]
)
def test_cancel_terminal_session_returns_409(
    client: TestClient, db: Session, status: SessionStatus
) -> None:
    user = _user(db)
    session = _session(db, user, status)
    _sign_in(client, db, user)

    response = client.post(f"/api/sessions/{session.id}/cancel", headers=_xsrf(client))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"
    db.refresh(session)
    assert session.status == status


def test_cancelled_session_frees_start(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _session(db, user)
    resume = _resume(db, user)
    _sign_in(client, db, user)

    cancel = client.post(f"/api/sessions/{session.id}/cancel", headers=_xsrf(client))
    start = _start(client, resume.id)

    assert cancel.status_code == 200
    assert start.status_code == 201
