"""Integration tests for the comparison route (TASK-060, plan section 8.1 "Reports").

Covers CMP-01, CMP-02 and AUTH-16 at the HTTP layer.
"""

import copy
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from http.cookies import SimpleCookie
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.responses import Response as StarletteResponse

from app.auth.sessions import create_auth_session
from app.config import get_settings
from app.db import get_db
from app.main import create_app
from app.models.account import User
from app.models.assessment import Report
from app.models.interview import InterviewSession, SessionStatus
from app.models.resume import Resume, ResumeStatus

NOT_FOUND_BODY = {"error": {"code": "RESOURCE_NOT_FOUND", "message": "Not found.", "details": None}}
ANSWER_MARKER = "candidate-answer-secret"

CONTENT: dict[str, Any] = {
    "adherence_percentage": "62.5",
    "skills": [
        {"skill": "Python", "average": "2.5", "question_positions": [1]},
        {"skill": "SQL", "average": "3.0", "question_positions": [2]},
    ],
    "items": [
        {"position": 1, "skill": "Python", "question": "Explain the GIL.", "answer": ANSWER_MARKER},
        {"position": 2, "skill": "SQL", "question": "What is a JOIN?", "answer": ANSWER_MARKER},
    ],
    "non_evaluated": {"nice_to_have": [], "non_technical": []},
    "plan": {"planned_count": 2, "skills": ["Python", "SQL"]},
    "model_version": "model-v1",
    "rubric_version": "rubric-v1",
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


def _session(db: Session, user: User, status: SessionStatus) -> InterviewSession:
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
    )
    db.add(session)
    db.flush()
    return session


def _report(
    db: Session,
    session: InterviewSession,
    content: dict[str, Any] | None = None,
    percentage: str = "62.5",
    rubric_version: str = "rubric-v1",
) -> Report:
    report = Report(
        session_id=session.id,
        content=copy.deepcopy(CONTENT) if content is None else content,
        adherence_percentage=Decimal(percentage),
        model_version="model-v1",
        rubric_version=rubric_version,
    )
    db.add(report)
    db.flush()
    return report


def _completed_with_report(db: Session, user: User, **report_args: Any) -> InterviewSession:
    session = _session(db, user, SessionStatus.COMPLETED)
    _report(db, session, **report_args)
    return session


def _compare(client: TestClient, a: Any, b: Any) -> Any:
    return client.get("/api/reports/compare", params={"a": str(a), "b": str(b)})


def test_cmp01_compare_returns_both_sides_and_common_skills(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    a = _completed_with_report(db, user, percentage="50.0")
    other = copy.deepcopy(CONTENT)
    other["skills"] = [
        {"skill": "sql", "average": "4.0", "question_positions": [2]},
        {"skill": "Go", "average": "1.0", "question_positions": [3]},
    ]
    b = _completed_with_report(db, user, content=other, percentage="75.0")
    _sign_in(client, db, user)

    response = _compare(client, a.id, b.id)

    assert response.status_code == 200
    assert response.json() == {
        "a": {
            "session_id": str(a.id),
            "percentage": "50.0",
            "rubric_version": "rubric-v1",
            "model_version": "model-v1",
        },
        "b": {
            "session_id": str(b.id),
            "percentage": "75.0",
            "rubric_version": "rubric-v1",
            "model_version": "model-v1",
        },
        "common_skills": [{"skill": "SQL", "a_average": "3.0", "b_average": "4.0"}],
        "comparable": True,
        "differences": [],
    }
    assert ANSWER_MARKER not in response.text


def test_cmp02_different_rubric_versions_return_warning(client: TestClient, db: Session) -> None:
    user = _user(db)
    a = _completed_with_report(db, user, rubric_version="rubric-v1")
    b = _completed_with_report(db, user, rubric_version="rubric-v2")
    _sign_in(client, db, user)

    response = _compare(client, a.id, b.id)

    assert response.status_code == 200
    body = response.json()
    assert body["comparable"] is False
    assert "rubric_version" in body["differences"]
    assert body["b"]["rubric_version"] == "rubric-v2"


def test_cmp01_compare_does_not_rewrite_stored_reports(client: TestClient, db: Session) -> None:
    user = _user(db)
    a = _completed_with_report(db, user)
    b = _completed_with_report(db, user, rubric_version="rubric-v2")
    _sign_in(client, db, user)

    assert _compare(client, a.id, b.id).status_code == 200

    db.expire_all()
    for session in (a, b):
        stored = db.query(Report).filter(Report.session_id == session.id).one()
        assert stored.content == CONTENT
        assert stored.adherence_percentage == Decimal("62.5")


@pytest.mark.parametrize(
    "status",
    [
        SessionStatus.IN_INTERVIEW,
        SessionStatus.EVALUATING,
        SessionStatus.EVALUATION_FAILED,
        SessionStatus.CANCELLED,
    ],
)
@pytest.mark.parametrize("side", ["a", "b"])
def test_cmp01_not_completed_session_is_409(
    client: TestClient, db: Session, status: SessionStatus, side: str
) -> None:
    user = _user(db)
    completed = _completed_with_report(db, user)
    pending = _session(db, user, status)
    _report(db, pending)
    _sign_in(client, db, user)

    ids = (pending.id, completed.id) if side == "a" else (completed.id, pending.id)
    response = _compare(client, *ids)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REPORT_NOT_AVAILABLE"
    assert ANSWER_MARKER not in response.text


def test_cmp01_completed_session_without_report_is_409(client: TestClient, db: Session) -> None:
    user = _user(db)
    a = _completed_with_report(db, user)
    b = _session(db, user, SessionStatus.COMPLETED)
    _sign_in(client, db, user)

    response = _compare(client, a.id, b.id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REPORT_NOT_AVAILABLE"


@pytest.mark.parametrize("side", ["a", "b"])
def test_auth16_session_of_other_user_is_uniform_404(
    client: TestClient, db: Session, side: str
) -> None:
    owner = _user(db)
    foreign = _completed_with_report(db, owner)
    intruder = _user(db)
    own = _completed_with_report(db, intruder)
    _sign_in(client, db, intruder)

    ids = (foreign.id, own.id) if side == "a" else (own.id, foreign.id)
    response = _compare(client, *ids)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY
    assert ANSWER_MARKER not in response.text


@pytest.mark.parametrize(
    "raw_id",
    ["not-a-uuid", str(uuid.uuid4()), "\x00", "abc\x01def", "", "1" * 5000],
    ids=["malformed", "unknown", "nul", "control", "empty", "long"],
)
def test_auth16_unknown_or_malformed_id_is_uniform_404(
    client: TestClient, db: Session, raw_id: str
) -> None:
    user = _user(db)
    own = _completed_with_report(db, user)
    _sign_in(client, db, user)

    response = client.get("/api/reports/compare", params={"a": str(own.id), "b": raw_id})

    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY


@pytest.mark.parametrize(
    "encoded", ["%ED%A0%80", "%FF%FE", "%00"], ids=["surrogate", "bad-utf8", "nul"]
)
def test_auth16_raw_encoded_id_is_uniform_404(
    client: TestClient, db: Session, encoded: str
) -> None:
    user = _user(db)
    own = _completed_with_report(db, user)
    _sign_in(client, db, user)

    response = client.get(f"/api/reports/compare?a={own.id}&b={encoded}")

    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY


@pytest.mark.parametrize("params", [{}, {"a": "x"}])
def test_compare_missing_parameter_is_validation_error(
    client: TestClient, db: Session, params: dict[str, str]
) -> None:
    user = _user(db)
    _sign_in(client, db, user)

    response = client.get("/api/reports/compare", params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_auth16_compare_requires_authentication(client: TestClient, db: Session) -> None:
    owner = _user(db)
    a = _completed_with_report(db, owner)
    b = _completed_with_report(db, owner)
    db.commit()
    client.cookies.clear()

    response = _compare(client, a.id, b.id)

    assert response.status_code == 401
    assert ANSWER_MARKER not in response.text
