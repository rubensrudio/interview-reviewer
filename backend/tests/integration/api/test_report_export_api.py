"""Integration tests for the report PDF export route (TASK-059, plan section 8.1 "Reports").

Covers EXPT-01, EXPT-02 and AUTH-16 at the HTTP layer.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from http.cookies import SimpleCookie
from io import BytesIO
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
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
from app.reports.builder import (
    REPORT_DISCLAIMER,
    NonEvaluated,
    ReportContent,
    ReportItem,
    ReportPlan,
    SkillPerformance,
)

NOT_FOUND_BODY = {"error": {"code": "RESOURCE_NOT_FOUND", "message": "Not found.", "details": None}}
QUESTION_TEXT = "What does the GIL protect?"


def _stored_content() -> dict[str, Any]:
    content = ReportContent(
        summary="You answered 1 question.",
        adherence_percentage="75.0",
        disclaimer=REPORT_DISCLAIMER,
        skills=[SkillPerformance(skill="Python", average="3.0", question_positions=[1])],
        items=[
            ReportItem(
                position=1,
                skill="Python",
                question=QUESTION_TEXT,
                answer="Interpreter state.",
                score=3,
                justification="Correct.",
                evidence_quotes=[],
                satisfactory=True,
                gap_explanation=None,
                reference_answer=None,
                no_verified_source=False,
            )
        ],
        unsatisfactory_items=[],
        non_evaluated=NonEvaluated(nice_to_have=[], non_technical=[]),
        plan=ReportPlan(planned_count=1, skills=["Python"]),
        model_version="model-v1",
        rubric_version="rubric-v1",
        sources_used=[],
        completed_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    return content.model_dump(mode="json")


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    app = create_app()

    def override_db() -> Iterator[Session]:
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


def _report(db: Session, session: InterviewSession, content: dict[str, Any]) -> Report:
    report = Report(
        session_id=session.id,
        content=content,
        adherence_percentage=Decimal("75.0"),
        model_version="model-v1",
        rubric_version="rubric-v1",
    )
    db.add(report)
    db.flush()
    return report


def _path(session_id: Any) -> str:
    return f"/api/sessions/{session_id}/report.pdf"


def _text(pdf: bytes) -> str:
    reader = PdfReader(BytesIO(pdf))
    return " ".join(" ".join(page.extract_text() or "" for page in reader.pages).split())


def test_expt01_completed_report_is_exported_as_pdf(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _session(db, user, SessionStatus.COMPLETED)
    _report(db, session, _stored_content())
    _sign_in(client, db, user)

    response = client.get(_path(session.id))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]
    text = _text(response.content)
    assert "75.0%" in text
    assert QUESTION_TEXT in text


@pytest.mark.parametrize(
    "status",
    [
        SessionStatus.IN_INTERVIEW,
        SessionStatus.EVALUATING,
        SessionStatus.EVALUATION_FAILED,
        SessionStatus.CANCELLED,
    ],
)
def test_expt02_export_of_not_completed_session_is_409(
    client: TestClient, db: Session, status: SessionStatus
) -> None:
    user = _user(db)
    session = _session(db, user, status)
    _report(db, session, _stored_content())
    _sign_in(client, db, user)

    response = client.get(_path(session.id))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REPORT_NOT_AVAILABLE"
    assert QUESTION_TEXT not in response.text


def test_expt02_completed_session_without_report_is_409(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _session(db, user, SessionStatus.COMPLETED)
    _sign_in(client, db, user)

    response = client.get(_path(session.id))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REPORT_NOT_AVAILABLE"


def test_expt02_stored_content_not_matching_schema_is_409(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    session = _session(db, user, SessionStatus.COMPLETED)
    _report(db, session, {"summary": "legacy shape"})
    _sign_in(client, db, user)

    response = client.get(_path(session.id))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REPORT_NOT_AVAILABLE"


def test_auth16_other_user_export_is_uniform_404(client: TestClient, db: Session) -> None:
    owner = _user(db)
    session = _session(db, owner, SessionStatus.COMPLETED)
    _report(db, session, _stored_content())
    intruder = _user(db)
    _sign_in(client, db, intruder)

    response = client.get(_path(session.id))

    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY


@pytest.mark.parametrize("session_id", ["not-a-uuid", str(uuid.uuid4()), "%00", "%ED%A0%80"])
def test_auth16_unknown_or_malformed_id_export_is_uniform_404(
    client: TestClient, db: Session, session_id: str
) -> None:
    user = _user(db)
    _sign_in(client, db, user)

    response = client.get(_path(session_id))

    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY


def test_auth16_export_requires_authentication(client: TestClient, db: Session) -> None:
    owner = _user(db)
    session = _session(db, owner, SessionStatus.COMPLETED)
    _report(db, session, _stored_content())
    db.commit()
    client.cookies.clear()

    response = client.get(_path(session.id))

    assert response.status_code == 401
