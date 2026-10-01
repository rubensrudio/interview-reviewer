"""Integration tests for the report route (TASK-058, plan section 8.1 "Reports").

Covers DATA-02, EVAL-08, EVAL-12, INTV-14 and AUTH-16 at the HTTP layer.
"""

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
REFERENCE_MARKER = "reference-answer-secret"

STORED_CONTENT: dict[str, Any] = {
    "summary": "You answered 2 questions; 1 needs improvement.",
    "adherence_percentage": "62.5",
    "disclaimer": "The percentage refers to the answers of this session only.",
    "skills": [{"skill": "Python", "percentage": "62.5"}],
    "items": [
        {
            "question": "Explain the GIL.",
            "answer": "It is a lock.",
            "score": 1,
            "justification": "Partial.",
            "reference_answer": REFERENCE_MARKER,
        }
    ],
    "non_evaluated": [],
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


def _report(db: Session, session: InterviewSession) -> Report:
    report = Report(
        session_id=session.id,
        content=STORED_CONTENT,
        adherence_percentage=Decimal("62.5"),
        model_version="model-v1",
        rubric_version="rubric-v1",
    )
    db.add(report)
    db.flush()
    return report


def _path(session_id: Any) -> str:
    return f"/api/sessions/{session_id}/report"


def test_data02_eval12_report_returns_stored_content_unchanged(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    session = _session(db, user, SessionStatus.COMPLETED)
    _report(db, session)
    _sign_in(client, db, user)

    first = client.get(_path(session.id))
    second = client.get(_path(session.id))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == STORED_CONTENT
    assert second.json() == first.json()
    assert second.content == first.content


def test_eval12_report_read_does_not_rewrite_stored_row(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _session(db, user, SessionStatus.COMPLETED)
    report = _report(db, session)
    created_at = report.created_at
    _sign_in(client, db, user)

    assert client.get(_path(session.id)).status_code == 200

    db.expire_all()
    stored = db.get(Report, report.id)
    assert stored is not None
    assert stored.content == STORED_CONTENT
    assert stored.created_at == created_at


@pytest.mark.parametrize(
    "status",
    [
        SessionStatus.IN_INTERVIEW,
        SessionStatus.EVALUATING,
        SessionStatus.EVALUATION_FAILED,
        SessionStatus.CANCELLED,
    ],
)
def test_intv14_report_of_open_session_is_409_without_reference(
    client: TestClient, db: Session, status: SessionStatus
) -> None:
    user = _user(db)
    session = _session(db, user, status)
    _sign_in(client, db, user)

    response = client.get(_path(session.id))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REPORT_NOT_AVAILABLE"
    assert REFERENCE_MARKER not in response.text


def test_intv14_report_row_of_not_completed_session_is_not_leaked(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    session = _session(db, user, SessionStatus.IN_INTERVIEW)
    _report(db, session)
    _sign_in(client, db, user)

    response = client.get(_path(session.id))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REPORT_NOT_AVAILABLE"
    assert REFERENCE_MARKER not in response.text


def test_eval08_completed_session_without_report_is_409(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _session(db, user, SessionStatus.COMPLETED)
    _sign_in(client, db, user)

    response = client.get(_path(session.id))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REPORT_NOT_AVAILABLE"


def test_auth16_other_user_report_is_uniform_404(client: TestClient, db: Session) -> None:
    owner = _user(db)
    session = _session(db, owner, SessionStatus.COMPLETED)
    _report(db, session)
    intruder = _user(db)
    _sign_in(client, db, intruder)

    response = client.get(_path(session.id))

    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY
    assert REFERENCE_MARKER not in response.text


@pytest.mark.parametrize("session_id", ["not-a-uuid", str(uuid.uuid4())])
def test_auth16_unknown_or_malformed_id_is_uniform_404(
    client: TestClient, db: Session, session_id: str
) -> None:
    user = _user(db)
    _sign_in(client, db, user)

    response = client.get(_path(session_id))

    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY


def test_auth16_report_requires_authentication(client: TestClient, db: Session) -> None:
    owner = _user(db)
    session = _session(db, owner, SessionStatus.COMPLETED)
    _report(db, session)
    db.commit()
    client.cookies.clear()

    response = client.get(_path(session.id))

    assert response.status_code == 401
    assert REFERENCE_MARKER not in response.text
