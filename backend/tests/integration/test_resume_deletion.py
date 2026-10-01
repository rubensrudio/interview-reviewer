"""Integration tests for resume version deletion (TASK-061, CT-51).

Covers DATA-03/DATA-04 (row, text, extraction and file deleted; sessions keep only the minimal
snapshot), DATA-90 (an unfinished session keeps working on the minimal snapshot), CV-96 (a
version deleted while ``processing`` never gets an extraction) and AUTH-16 at the HTTP layer.
"""

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import pytest
from fakes.fake_llm import FakeLLM
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from starlette.responses import Response as StarletteResponse

from app.auth.sessions import XSRF_COOKIE, XSRF_HEADER, create_auth_session
from app.config import get_settings
from app.db import get_db, get_sessionmaker
from app.errors import RESOURCE_NOT_FOUND, AppError
from app.main import create_app
from app.models.account import User
from app.models.assessment import Answer, Report
from app.models.interview import InterviewSession, Question, SessionStatus
from app.models.resume import ExtractionItem, Resume, ResumeStatus
from app.privacy.resume_deletion import delete_resume
from app.resumes import processing, storage
from app.resumes.extraction import EXTRACTION_TASK
from app.resumes.processing import process_resume

PDF_BYTES = b"%PDF-1.4\n" + b"a" * 512

SKILL_WITH_EVIDENCE = ExtractionItem(
    id="skill-1",
    kind="skill",
    fields={"name": "Python", "description": "Built services at Acme Corp"},
    origin="explicit",
    evidence=["Python at Acme Corp"],
).model_dump(mode="json")
SKILL_WITHOUT_EVIDENCE = ExtractionItem(
    id="skill-2", kind="skill", fields={"name": "Go"}, origin="user_provided", evidence=[]
).model_dump(mode="json")
EXPERIENCE = ExtractionItem(
    id="exp-1",
    kind="experience",
    fields={"title": "Backend Engineer", "organization": "Acme Corp"},
    origin="explicit",
    evidence=["Backend Engineer at Acme Corp"],
).model_dump(mode="json")
EDUCATION = ExtractionItem(
    id="edu-1",
    kind="education",
    fields={"degree": "BSc", "institution": "Lakeside University"},
    origin="explicit",
    evidence=["BSc at Lakeside University"],
).model_dump(mode="json")
FULL_EXTRACTION = [EXPERIENCE, SKILL_WITH_EVIDENCE, EDUCATION, SKILL_WITHOUT_EVIDENCE]
MINIMAL_SNAPSHOT = [
    {
        "id": "skill-1",
        "kind": "skill",
        "fields": {"name": "Python"},
        "origin": "explicit",
        "evidence": ["Python at Acme Corp"],
    }
]

ENGLISH_PDF_LINES = [
    "Riley Placeholder - Senior Backend Engineer",
    "Experience: Backend Engineer at Northwind Logistics from 2019 to 2023.",
    "Built order routing services in Java and Spring Boot backed by PostgreSQL.",
    "Designed REST APIs consumed by thousands of warehouses every single day.",
    "Skills: Java, Spring Boot, PostgreSQL, Docker, Kubernetes, testing and CI/CD.",
]
VALID_EXTRACTION = {
    "items": [
        {
            "kind": "skill",
            "fields": {"name": "Java"},
            "origin": "explicit",
            "evidence": ["Skills: Java, Spring Boot"],
        }
    ]
}


@pytest.fixture(autouse=True)
def storage_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "storage"
    monkeypatch.setenv("IR_STORAGE_DIR", str(root))
    monkeypatch.delenv("IR_OCR_ENABLED", raising=False)
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


# --- helpers -------------------------------------------------------------------------------


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


def _resume(
    db: Session,
    user: User,
    status: ResumeStatus = ResumeStatus.READY,
    data: bytes = PDF_BYTES,
    extraction: list[dict[str, Any]] | None = None,
) -> Resume:
    resume_id = uuid.uuid4()
    ready = status is ResumeStatus.READY
    resume = Resume(
        id=resume_id,
        user_id=user.id,
        filename="Jane Doe resume.pdf",
        status=status,
        storage_key=storage.save_file(user.id, resume_id, data),
        extracted_text="Jane Doe, Backend Engineer at Acme Corp" if ready else None,
        extraction=(FULL_EXTRACTION if extraction is None else extraction) if ready else None,
    )
    db.add(resume)
    db.flush()
    return resume


def _session(
    db: Session,
    user: User,
    resume: Resume,
    status: SessionStatus = SessionStatus.IN_INTERVIEW,
) -> InterviewSession:
    session = InterviewSession(
        user_id=user.id,
        resume_id=resume.id,
        resume_name=resume.filename,
        status=status,
        snapshot=list(resume.extraction or []),
        planned_count=2,
    )
    db.add(session)
    db.flush()
    return session


def _question(db: Session, session: InterviewSession, position: int) -> Question:
    question = Question(
        session_id=session.id, position=position, skill_name="Python", text="Explain the GIL."
    )
    db.add(question)
    db.flush()
    return question


def _file_path(storage_dir: Path, resume: Resume) -> Path:
    return storage_dir / str(resume.user_id) / f"{resume.id}.pdf"


def _resume_exists(db: Session, resume_id: uuid.UUID) -> bool:
    db.expire_all()
    found = db.execute(select(Resume.id).where(Resume.id == resume_id)).scalar_one_or_none()
    return found is not None


def _reload_session(db: Session, session_id: uuid.UUID) -> InterviewSession:
    db.expire_all()
    return db.execute(
        select(InterviewSession).where(InterviewSession.id == session_id)
    ).scalar_one()


# --- DATA-04: row, text, extraction and file ------------------------------------------------


def test_data_04_delete_removes_row_and_file_after_commit(db: Session, storage_dir: Path) -> None:
    user = _user(db)
    resume = _resume(db, user)
    path = _file_path(storage_dir, resume)
    assert path.exists()

    delete_resume(db, user, resume.id)
    db.commit()

    assert not _resume_exists(db, resume.id)
    assert not path.exists()
    assert list(storage_dir.rglob("*.pdf")) == []


def test_data_04_file_is_kept_until_commit_and_after_rollback(
    db: Session, storage_dir: Path
) -> None:
    user = _user(db)
    resume = _resume(db, user)
    db.commit()
    path = _file_path(storage_dir, resume)

    delete_resume(db, user, resume.id)
    # Not committed yet: the file must still be there.
    assert path.exists()
    db.rollback()

    assert path.exists()
    assert _resume_exists(db, resume.id)
    # A later, unrelated commit must not delete the file of the rolled-back deletion.
    db.add(User(email_normalized=f"other-{uuid.uuid4().hex}@example.com"))
    db.commit()
    assert path.exists()


def test_data_04_only_the_requested_version_is_deleted(db: Session, storage_dir: Path) -> None:
    user = _user(db)
    target = _resume(db, user)
    kept = _resume(db, user)

    delete_resume(db, user, target.id)
    db.commit()

    assert not _resume_exists(db, target.id)
    assert _resume_exists(db, kept.id)
    assert _file_path(storage_dir, kept).exists()


def test_data_04_other_users_version_raises_not_found_and_deletes_nothing(
    db: Session, storage_dir: Path
) -> None:
    owner = _user(db)
    intruder = _user(db)
    resume = _resume(db, owner)
    session = _session(db, owner, resume)

    with pytest.raises(AppError) as exc_info:
        delete_resume(db, intruder, resume.id)
    db.commit()

    assert exc_info.value.code == RESOURCE_NOT_FOUND
    assert _resume_exists(db, resume.id)
    assert _file_path(storage_dir, resume).exists()
    reloaded = _reload_session(db, session.id)
    assert reloaded.snapshot == FULL_EXTRACTION
    assert reloaded.snapshot_minimal is False


def test_data_04_unknown_version_raises_not_found(db: Session) -> None:
    user = _user(db)

    with pytest.raises(AppError) as exc_info:
        delete_resume(db, user, uuid.uuid4())

    assert exc_info.value.code == RESOURCE_NOT_FOUND


# --- DATA-04 / DATA-90 / LAC-11: minimal snapshot ------------------------------------------


def test_data_90_in_interview_session_keeps_minimal_snapshot_and_accepts_answers(
    db: Session,
) -> None:
    user = _user(db)
    resume = _resume(db, user)
    session = _session(db, user, resume, SessionStatus.IN_INTERVIEW)
    first, second = _question(db, session, 1), _question(db, session, 2)
    db.add(Answer(session_id=session.id, question_id=first.id, content="A", idempotency_key="k1"))
    db.commit()

    delete_resume(db, user, resume.id)
    db.commit()

    reloaded = _reload_session(db, session.id)
    assert reloaded.snapshot == MINIMAL_SNAPSHOT
    assert all(item["kind"] == "skill" and item["evidence"] for item in reloaded.snapshot)
    assert reloaded.snapshot_minimal is True
    assert reloaded.resume_name is None
    assert reloaded.resume_id is None
    assert reloaded.status is SessionStatus.IN_INTERVIEW
    # Each minimal item is still a valid ExtractionItem for the evaluation pipeline.
    for item in reloaded.snapshot:
        ExtractionItem.model_validate(item)

    # The session keeps accepting answers (DATA-90).
    db.add(Answer(session_id=session.id, question_id=second.id, content="B", idempotency_key="k2"))
    db.commit()
    answers = db.execute(
        select(func.count()).select_from(Answer).where(Answer.session_id == session.id)
    ).scalar_one()
    assert answers == 2


def test_lac_11_completed_session_and_report_are_kept_with_minimal_snapshot(
    db: Session,
) -> None:
    user = _user(db)
    resume = _resume(db, user)
    session = _session(db, user, resume, SessionStatus.COMPLETED)
    question = _question(db, session, 1)
    db.add(Answer(session_id=session.id, question_id=question.id, content="A", idempotency_key="k"))
    db.add(
        Report(
            session_id=session.id,
            content={"summary": "ok"},
            adherence_percentage=Decimal("50.0"),
            model_version="m",
            rubric_version="r",
        )
    )
    db.commit()

    delete_resume(db, user, resume.id)
    db.commit()

    reloaded = _reload_session(db, session.id)
    assert reloaded.status is SessionStatus.COMPLETED
    assert reloaded.snapshot == MINIMAL_SNAPSHOT
    assert reloaded.snapshot_minimal is True
    reports = db.execute(
        select(func.count()).select_from(Report).where(Report.session_id == session.id)
    ).scalar_one()
    answers = db.execute(
        select(func.count()).select_from(Answer).where(Answer.session_id == session.id)
    ).scalar_one()
    assert (reports, answers) == (1, 1)


def test_data_04_sessions_of_other_versions_are_untouched(db: Session) -> None:
    user = _user(db)
    target = _resume(db, user)
    other = _resume(db, user)
    touched = _session(db, user, target, SessionStatus.CANCELLED)
    untouched = _session(db, user, other, SessionStatus.IN_INTERVIEW)

    delete_resume(db, user, target.id)
    db.commit()

    assert _reload_session(db, touched.id).snapshot_minimal is True
    kept = _reload_session(db, untouched.id)
    assert kept.snapshot == FULL_EXTRACTION
    assert kept.snapshot_minimal is False
    assert kept.resume_name == "Jane Doe resume.pdf"
    assert kept.resume_id == other.id


def test_data_04_every_session_that_used_the_version_is_reduced(db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user)
    sessions = [
        _session(db, user, resume, status)
        for status in (SessionStatus.COMPLETED, SessionStatus.EXPIRED, SessionStatus.IN_INTERVIEW)
    ]

    delete_resume(db, user, resume.id)
    db.commit()

    for session in sessions:
        reloaded = _reload_session(db, session.id)
        assert reloaded.snapshot == MINIMAL_SNAPSHOT
        assert reloaded.snapshot_minimal is True
        assert reloaded.resume_name is None


# --- CV-96: version in processing ----------------------------------------------------------


def _english_pdf() -> bytes:
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    y = 800
    for line in ENGLISH_PDF_LINES:
        pdf.drawString(50, y, line)
        y -= 20
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def test_cv_96_processing_version_deleted_then_job_persists_no_extraction(
    db: Session, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM({EXTRACTION_TASK: [VALID_EXTRACTION]})
    monkeypatch.setattr(processing, "get_llm_client", lambda: llm)
    user = _user(db)
    resume = _resume(db, user, ResumeStatus.PROCESSING, data=_english_pdf())
    resume_id = resume.id

    delete_resume(db, user, resume_id)
    db.commit()
    process_resume(db, {"resume_id": str(resume_id)})
    db.commit()

    assert not _resume_exists(db, resume_id)
    assert llm.calls == []
    assert list(storage_dir.rglob("*.pdf")) == []


def test_cv_96_deletion_during_inference_discards_the_result(
    db: Session, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(db)
    resume = _resume(db, user, ResumeStatus.PROCESSING, data=_english_pdf())
    resume_id = resume.id

    class DeletingLLM(FakeLLM):
        def complete_structured(self, task, system, user_prompt, output_model):  # type: ignore[no-untyped-def,override]
            delete_resume(db, user, resume_id)
            db.commit()
            return super().complete_structured(task, system, user_prompt, output_model)

    llm = DeletingLLM({EXTRACTION_TASK: [VALID_EXTRACTION]})
    monkeypatch.setattr(processing, "get_llm_client", lambda: llm)

    process_resume(db, {"resume_id": str(resume_id)})
    db.commit()

    assert len(llm.calls) == 1
    assert not _resume_exists(db, resume_id)
    assert list(storage_dir.rglob("*.pdf")) == []


@pytest.fixture
def committed_users(migrated_database: str) -> Iterator[list[uuid.UUID]]:
    user_ids: list[uuid.UUID] = []
    yield user_ids
    with get_sessionmaker()() as session:
        session.execute(delete(User).where(User.id.in_(user_ids)))
        session.commit()


def test_cv_96_concurrent_deletion_while_job_runs_with_real_commits(
    committed_users: list[uuid.UUID], storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with get_sessionmaker()() as setup:
        owner = _user(setup)
        resume = _resume(setup, owner, ResumeStatus.RECEIVED, data=_english_pdf())
        setup.commit()
        user_id, resume_id = owner.id, resume.id
    committed_users.append(user_id)

    inference_started = threading.Event()
    deletion_done = threading.Event()
    errors: list[BaseException] = []

    class BlockingLLM(FakeLLM):
        def complete_structured(self, task, system, user_prompt, output_model):  # type: ignore[no-untyped-def,override]
            inference_started.set()
            assert deletion_done.wait(timeout=20)
            return super().complete_structured(task, system, user_prompt, output_model)

    llm = BlockingLLM({EXTRACTION_TASK: [VALID_EXTRACTION]})
    monkeypatch.setattr(processing, "get_llm_client", lambda: llm)

    def run_job() -> None:
        try:
            with get_sessionmaker()() as worker:
                process_resume(worker, {"resume_id": str(resume_id)})
                worker.commit()
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(error)

    def run_deletion() -> None:
        try:
            assert inference_started.wait(timeout=20)
            with get_sessionmaker()() as session:
                deleter = session.get(User, user_id)
                assert deleter is not None
                delete_resume(session, deleter, resume_id)
                session.commit()
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(error)
        finally:
            deletion_done.set()

    threads = [threading.Thread(target=run_job), threading.Thread(target=run_deletion)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    with get_sessionmaker()() as check:
        assert check.get(Resume, resume_id) is None
    assert list(storage_dir.rglob("*.pdf")) == []


# --- HTTP: DELETE /api/resumes/{id} --------------------------------------------------------


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


def test_data_04_api_delete_own_version_returns_204(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    user = _user(db)
    resume = _resume(db, user)
    session = _session(db, user, resume)
    _sign_in(client, db, user)

    response = client.delete(f"/api/resumes/{resume.id}", headers=_xsrf(client))

    assert response.status_code == 204
    assert response.content == b""
    assert not _resume_exists(db, resume.id)
    assert not _file_path(storage_dir, resume).exists()
    assert _reload_session(db, session.id).snapshot_minimal is True


def test_auth_16_api_delete_other_users_version_returns_404_and_deletes_nothing(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    owner = _user(db)
    intruder = _user(db)
    resume = _resume(db, owner)
    session = _session(db, owner, resume)
    _sign_in(client, db, intruder)

    response = client.delete(f"/api/resumes/{resume.id}", headers=_xsrf(client))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == RESOURCE_NOT_FOUND
    assert "Jane" not in response.text
    assert _resume_exists(db, resume.id)
    assert _file_path(storage_dir, resume).exists()
    reloaded = _reload_session(db, session.id)
    assert reloaded.snapshot_minimal is False
    assert reloaded.resume_name == "Jane Doe resume.pdf"


def test_auth_16_api_delete_malformed_id_returns_404(client: TestClient, db: Session) -> None:
    user = _user(db)
    _sign_in(client, db, user)

    response = client.delete("/api/resumes/not-a-uuid", headers=_xsrf(client))

    assert response.status_code == 404


def test_api_delete_requires_csrf_header(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    user = _user(db)
    resume = _resume(db, user)
    _sign_in(client, db, user)

    response = client.delete(f"/api/resumes/{resume.id}")

    assert response.status_code == 403
    assert _resume_exists(db, resume.id)
    assert _file_path(storage_dir, resume).exists()
