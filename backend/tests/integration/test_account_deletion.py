"""Integration tests for account deletion (TASK-063, CT-53).

Covers DATA-06 (every row and file of the account is deleted at once, the authenticated
sessions end and the response mentions the 30-day backup expiry), DATA-07 (a new sign-up with
the same e-mail gets a brand-new account) and DATA-93 (a deletion interrupted by a storage
failure leaves the account unusable and the ``account.purge`` job finishes it, including with
the worker and real commits).
"""

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import cast, delete, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from starlette.responses import Response as StarletteResponse

from app.auth import registration
from app.auth.login import login_local
from app.auth.passwords import hash_password
from app.auth.registration import register_local
from app.auth.sessions import (
    SESSION_COOKIE,
    XSRF_COOKIE,
    XSRF_HEADER,
    create_auth_session,
)
from app.auth.tokens import issue_one_time_token
from app.config import get_settings
from app.db import Base, get_db, get_sessionmaker
from app.email.templates import EmailContent
from app.errors import INVALID_CREDENTIALS, AppError
from app.jobs.registry import JobRegistry, default_registry
from app.jobs.worker import Worker
from app.main import create_app
from app.models.account import AuthSession, OneTimeToken, TokenPurpose, User
from app.models.assessment import Answer, Evaluation, Report
from app.models.interview import InterviewSession, Question, SessionStatus
from app.models.job import Job, JobStatus
from app.models.resume import Resume, ResumeStatus
from app.privacy import account_deletion
from app.privacy.account_deletion import (
    ACCOUNT_DELETED_MESSAGE,
    ACCOUNT_PURGE_JOB,
    delete_account,
    purge_account,
)
from app.resumes import storage

PASSWORD = "correct horse battery staple 42"  # noqa: S105 - test fixture value
PDF_BYTES = b"%PDF-1.4\n% account deletion test\n%%EOF\n"
BACKUP_NOTICE = "Backup copies expire within 30 days"


@pytest.fixture(autouse=True)
def storage_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "storage"
    monkeypatch.setenv("IR_STORAGE_DIR", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def __call__(self, to: str, message: EmailContent) -> bool:
        self.sent.append(to)
        return True


@pytest.fixture
def mailer(monkeypatch: pytest.MonkeyPatch) -> FakeMailer:
    fake = FakeMailer()
    monkeypatch.setattr(registration, "send_email", fake)
    return fake


class StorageDown(OSError):
    """Simulated storage outage."""


def _failing_delete_user_files(user_id: uuid.UUID) -> None:
    raise StorageDown("storage unavailable")


# --- helpers -------------------------------------------------------------------------------


def _user(db: Session, email: str | None = None) -> User:
    settings = get_settings()
    user = User(
        email_normalized=email or f"user-{uuid.uuid4().hex}@example.com",
        password_hash=hash_password(PASSWORD),
        email_verified_at=datetime.now(UTC),
        google_sub=f"google-{uuid.uuid4().hex}",
        terms_version=settings.terms_version,
        privacy_version=settings.privacy_version,
        terms_accepted_at=datetime.now(UTC),
    )
    db.add(user)
    db.flush()
    return user


def _resume(db: Session, user: User) -> Resume:
    resume_id = uuid.uuid4()
    resume = Resume(
        id=resume_id,
        user_id=user.id,
        filename="Jane Doe resume.pdf",
        status=ResumeStatus.READY,
        storage_key=storage.save_file(user.id, resume_id, PDF_BYTES),
        extracted_text="Jane Doe, Backend Engineer at Acme Corp",
        extraction=[],
    )
    db.add(resume)
    db.flush()
    return resume


def _completed_session(db: Session, user: User, resume: Resume) -> InterviewSession:
    session = InterviewSession(
        user_id=user.id,
        resume_id=resume.id,
        status=SessionStatus.COMPLETED,
        resume_name=resume.filename,
        requirement_items=[],
        proposal={"planned_count": 1, "skills": ["Python"]},
        planned_count=1,
        answered_count=1,
        snapshot=[],
        completed_at=datetime.now(UTC),
    )
    db.add(session)
    db.flush()
    question = Question(
        session_id=session.id,
        position=1,
        skill_name="Python",
        text="Explain Python.",
        reference_points=["Point"],
        sources=[],
        no_verified_source=True,
    )
    db.add(question)
    db.flush()
    answer = Answer(
        session_id=session.id,
        question_id=question.id,
        content="My answer",
        idempotency_key="key-1",
    )
    db.add(answer)
    db.flush()
    db.add(
        Evaluation(
            answer_id=answer.id,
            score=4,
            justification="Good answer.",
            evidence_quotes=[],
            model_version="model+1",
        )
    )
    db.add(
        Report(
            session_id=session.id,
            content={"adherence_percentage": "100.0"},
            adherence_percentage=Decimal("100.0"),
            model_version="model+1",
            rubric_version="rubric-1",
        )
    )
    db.flush()
    return session


def _full_account(db: Session, email: str | None = None) -> tuple[User, Resume, InterviewSession]:
    user = _user(db, email)
    resume = _resume(db, user)
    session = _completed_session(db, user, resume)
    issue_one_time_token(db, user.id, TokenPurpose.VERIFY_EMAIL, timedelta(hours=1))
    return user, resume, session


def _rows_referencing(db: Session, user_id: uuid.UUID) -> dict[str, int]:
    """Count, in every table, the rows that hold ``user_id`` (FK columns, ids and job payloads)."""
    db.expire_all()
    found: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            is_user_ref = column.name == "user_id" or (
                table.name == "users" and column.name == "id"
            )
            if is_user_ref:
                count = db.execute(
                    select(func.count()).select_from(table).where(column == user_id)
                ).scalar_one()
                if count:
                    found[f"{table.name}.{column.name}"] = int(count)
    payload_hits = db.execute(
        select(func.count())
        .select_from(Job)
        .where(cast(Job.payload, JSONB).op("@>")(cast({"user_id": str(user_id)}, JSONB)))
    ).scalar_one()
    if payload_hits:
        found["jobs.payload"] = int(payload_hits)
    return found


def _user_exists(db: Session, user_id: uuid.UUID) -> bool:
    db.expire_all()
    return db.execute(select(User.id).where(User.id == user_id)).scalar_one_or_none() is not None


def _purge_jobs(db: Session, user_id: uuid.UUID, status: JobStatus | None = None) -> list[Job]:
    db.expire_all()
    statement = select(Job).where(
        Job.kind == ACCOUNT_PURGE_JOB, Job.payload["user_id"].astext == str(user_id)
    )
    if status is not None:
        statement = statement.where(Job.status == status)
    return list(db.execute(statement).scalars().all())


def _count(db: Session, model: Any, user_id: uuid.UUID) -> int:
    db.expire_all()
    return int(
        db.execute(
            select(func.count()).select_from(model).where(model.user_id == user_id)
        ).scalar_one()
    )


# --- service: DATA-06 ----------------------------------------------------------------------


def test_data_06_delete_account_removes_every_row_and_the_user_directory(
    db: Session, storage_dir: Path
) -> None:
    user, _, session = _full_account(db)
    other, other_resume, _ = _full_account(db)
    create_auth_session(db, user, StarletteResponse())
    user_id, session_id = user.id, session.id
    assert (storage_dir / str(user_id)).is_dir()

    delete_account(db, user, StarletteResponse())

    assert _rows_referencing(db, user_id) == {}
    assert not (storage_dir / str(user_id)).exists()
    reports = db.execute(
        select(func.count()).select_from(Report).where(Report.session_id == session_id)
    ).scalar_one()
    assert reports == 0
    # Nothing of another account is touched.
    assert _user_exists(db, other.id)
    assert _count(db, Resume, other.id) == 1
    assert (storage_dir / str(other.id) / f"{other_resume.id}.pdf").exists()


def test_data_06_delete_account_clears_the_session_cookies(db: Session) -> None:
    user = _user(db)
    response = StarletteResponse()

    delete_account(db, user, response)

    cookies = response.headers.getlist("set-cookie")
    assert any(c.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" in c for c in cookies)
    assert any(c.startswith(f"{XSRF_COOKIE}=") and "Max-Age=0" in c for c in cookies)


def test_data_06_user_without_files_is_deleted(db: Session, storage_dir: Path) -> None:
    user = _user(db)
    user_id = user.id

    delete_account(db, user, StarletteResponse())

    assert _rows_referencing(db, user_id) == {}


# --- service: DATA-93 ----------------------------------------------------------------------


def test_data_93_storage_failure_keeps_data_but_account_is_unusable(
    db: Session, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, resume, _ = _full_account(db)
    email, user_id = user.email_normalized, user.id
    create_auth_session(db, user, StarletteResponse())
    monkeypatch.setattr(storage, "delete_user_files", _failing_delete_user_files)

    delete_account(db, user, StarletteResponse())

    # Step 1 committed: deletion requested, every session revoked, purge job queued.
    db.expire_all()
    stored = db.get(User, user_id)
    assert stored is not None
    assert stored.deletion_requested_at is not None
    active = db.execute(
        select(func.count())
        .select_from(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
    ).scalar_one()
    assert active == 0
    assert len(_purge_jobs(db, user_id, JobStatus.QUEUED)) == 1
    # Steps 2-3 rolled back together: no partial deletion of rows.
    assert _count(db, Resume, user_id) == 1
    assert (storage_dir / str(user_id) / f"{resume.id}.pdf").exists()
    # The account cannot be used any more.
    with pytest.raises(AppError) as raised:
        login_local(db, email, PASSWORD, "127.0.0.1")
    assert raised.value.code == INVALID_CREDENTIALS


def test_data_93_later_purge_job_completes_the_deletion(
    db: Session, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, _, _ = _full_account(db)
    user_id = user.id
    with monkeypatch.context() as patch:
        patch.setattr(storage, "delete_user_files", _failing_delete_user_files)
        delete_account(db, user, StarletteResponse())
    assert _user_exists(db, user_id)

    purge_account(db, {"user_id": str(user_id)})
    db.commit()

    assert not _user_exists(db, user_id)
    assert not (storage_dir / str(user_id)).exists()
    assert _count(db, Resume, user_id) == 0
    assert _count(db, InterviewSession, user_id) == 0
    assert _count(db, OneTimeToken, user_id) == 0
    assert _count(db, AuthSession, user_id) == 0
    # The queued purge job was consumed by the purge; only the running one may remain.
    assert _purge_jobs(db, user_id, JobStatus.QUEUED) == []


def test_data_93_purge_job_with_storage_still_down_reschedules_itself(
    db: Session, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, resume, _ = _full_account(db)
    user_id = user.id
    monkeypatch.setattr(storage, "delete_user_files", _failing_delete_user_files)
    delete_account(db, user, StarletteResponse())
    first = _purge_jobs(db, user_id, JobStatus.QUEUED)
    assert len(first) == 1
    # The worker claims the queued job before running its handler.
    first[0].status = JobStatus.RUNNING
    db.flush()

    purge_account(db, {"user_id": str(user_id)})
    db.commit()

    assert _user_exists(db, user_id)
    assert _count(db, Resume, user_id) == 1
    assert (storage_dir / str(user_id) / f"{resume.id}.pdf").exists()
    queued = _purge_jobs(db, user_id, JobStatus.QUEUED)
    assert len(queued) == 1
    assert queued[0].id != first[0].id
    assert queued[0].run_after > datetime.now(UTC)


def test_data_93_reschedule_does_not_duplicate_a_pending_job(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(db)
    user_id = user.id
    monkeypatch.setattr(storage, "delete_user_files", _failing_delete_user_files)
    delete_account(db, user, StarletteResponse())

    purge_account(db, {"user_id": str(user_id)})
    purge_account(db, {"user_id": str(user_id)})
    db.commit()

    assert len(_purge_jobs(db, user_id, JobStatus.QUEUED)) == 1


def test_data_93_purge_of_unknown_user_is_a_no_op(db: Session) -> None:
    purge_account(db, {"user_id": str(uuid.uuid4())})
    db.commit()


def test_data_93_purge_never_deletes_an_account_without_a_deletion_request(
    db: Session, storage_dir: Path
) -> None:
    user, resume, _ = _full_account(db)
    user_id = user.id

    purge_account(db, {"user_id": str(user_id)})
    db.commit()

    assert _user_exists(db, user_id)
    assert _count(db, Resume, user_id) == 1
    assert (storage_dir / str(user_id) / f"{resume.id}.pdf").exists()


@pytest.mark.parametrize("payload", [{}, {"user_id": "not-a-uuid"}, {"user_id": ""}])
def test_data_93_purge_with_invalid_payload_raises_and_changes_nothing(
    db: Session, payload: dict[str, str]
) -> None:
    user = _user(db)

    with pytest.raises(ValueError):
        purge_account(db, payload)

    assert _user_exists(db, user.id)


def test_data_93_purge_job_is_registered_for_the_worker() -> None:
    assert default_registry.handler_for(ACCOUNT_PURGE_JOB) is purge_account


# --- DATA-07: new sign-up with the same e-mail ----------------------------------------------


def test_data_07_new_sign_up_with_the_same_email_creates_a_new_empty_account(
    db: Session, mailer: FakeMailer
) -> None:
    email = f"reuse-{uuid.uuid4().hex}@example.com"
    old, _, _ = _full_account(db, email)
    old_id = old.id
    delete_account(db, old, StarletteResponse())

    register_local(db, email, PASSWORD, accepted_terms=True)
    db.commit()

    db.expire_all()
    new = db.execute(select(User).where(User.email_normalized == email)).scalar_one()
    assert new.id != old_id
    assert new.google_sub is None
    assert new.deletion_requested_at is None
    assert _count(db, Resume, new.id) == 0
    assert _count(db, InterviewSession, new.id) == 0
    assert mailer.sent == [email]


# --- real commits: worker and concurrency (DATA-93) ----------------------------------------


@pytest.fixture
def committed_users(migrated_database: str) -> Iterator[list[uuid.UUID]]:
    user_ids: list[uuid.UUID] = []
    yield user_ids
    with get_sessionmaker()() as session:
        session.execute(delete(User).where(User.id.in_(user_ids)))
        session.execute(
            delete(Job).where(
                Job.kind == ACCOUNT_PURGE_JOB,
                Job.payload["user_id"].astext.in_([str(i) for i in user_ids]),
            )
        )
        session.commit()


def _committed_account() -> tuple[uuid.UUID, uuid.UUID]:
    with get_sessionmaker()() as setup:
        user, resume, _ = _full_account(setup)
        create_auth_session(setup, user, StarletteResponse())
        ids = (user.id, resume.id)
        setup.commit()
    return ids


def _make_due(user_id: uuid.UUID) -> None:
    with get_sessionmaker()() as session:
        session.execute(
            text(
                "UPDATE jobs SET run_after = clock_timestamp() - interval '1 second' "
                "WHERE kind = :kind AND payload->>'user_id' = :user_id AND status = 'queued'"
            ),
            {"kind": ACCOUNT_PURGE_JOB, "user_id": str(user_id)},
        )
        session.commit()


def _worker() -> Worker:
    registry = JobRegistry()
    registry.register(ACCOUNT_PURGE_JOB, purge_account)
    return Worker(registry=registry, reap_every_seconds=None)


def test_data_93_worker_completes_an_interrupted_deletion_with_real_commits(
    committed_users: list[uuid.UUID], storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, resume_id = _committed_account()
    committed_users.append(user_id)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "delete_user_files", _failing_delete_user_files)
        with get_sessionmaker()() as db:
            user = db.get(User, user_id)
            assert user is not None
            delete_account(db, user, StarletteResponse())

    with get_sessionmaker()() as check:
        assert _user_exists(check, user_id)
        assert (storage_dir / str(user_id) / f"{resume_id}.pdf").exists()

    _make_due(user_id)
    assert _worker().run_once() is True

    with get_sessionmaker()() as check:
        assert not _user_exists(check, user_id)
        assert _count(check, Resume, user_id) == 0
        jobs = _purge_jobs(check, user_id)
        assert [job.status for job in jobs] == [JobStatus.DONE]
    assert not (storage_dir / str(user_id)).exists()


def test_data_93_inline_purge_racing_the_worker_with_real_commits(
    committed_users: list[uuid.UUID], storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, _ = _committed_account()
    committed_users.append(user_id)
    # No grace period: the worker may claim the purge job while the request is still purging.
    monkeypatch.setattr(account_deletion, "INLINE_PURGE_GRACE", timedelta(0))
    requested = threading.Event()
    real_delete_user_files = storage.delete_user_files
    errors: list[BaseException] = []
    worked: list[bool] = []

    def slow_delete_user_files(target: uuid.UUID) -> None:
        requested.set()
        threading.Event().wait(0.3)
        real_delete_user_files(target)

    monkeypatch.setattr(storage, "delete_user_files", slow_delete_user_files)

    def run_request() -> None:
        try:
            with get_sessionmaker()() as db:
                user = db.get(User, user_id)
                assert user is not None
                delete_account(db, user, StarletteResponse())
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(error)

    def run_worker() -> None:
        try:
            assert requested.wait(timeout=20)
            worked.append(_worker().run_once())
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(error)

    threads = [threading.Thread(target=run_request), threading.Thread(target=run_worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    with get_sessionmaker()() as check:
        assert not _user_exists(check, user_id)
        assert _count(check, Resume, user_id) == 0
        assert _purge_jobs(check, user_id, JobStatus.QUEUED) == []
        assert _purge_jobs(check, user_id, JobStatus.FAILED) == []
    assert not (storage_dir / str(user_id)).exists()


def test_data_06_concurrent_account_deletions_with_real_commits(
    committed_users: list[uuid.UUID], storage_dir: Path
) -> None:
    user_id, _ = _committed_account()
    committed_users.append(user_id)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run_deletion() -> None:
        try:
            with get_sessionmaker()() as db:
                user = db.get(User, user_id)
                assert user is not None
                barrier.wait(timeout=10)
                delete_account(db, user, StarletteResponse())
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(error)

    threads = [threading.Thread(target=run_deletion) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    with get_sessionmaker()() as check:
        assert _rows_referencing(check, user_id) == {}
    assert not (storage_dir / str(user_id)).exists()


# --- HTTP: DELETE /api/account -------------------------------------------------------------


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


def test_data_06_api_delete_account_removes_everything_and_mentions_backups(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    user, _, _ = _full_account(db)
    user_id = user.id
    _sign_in(client, db, user)
    old_cookies = dict(client.cookies.items())

    response = client.delete("/api/account", headers=_xsrf(client))

    assert response.status_code == 200
    assert response.json() == {"message": ACCOUNT_DELETED_MESSAGE}
    assert BACKUP_NOTICE in response.json()["message"]
    set_cookies = response.headers.get_list("set-cookie")
    assert any(c.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" in c for c in set_cookies)
    assert _rows_referencing(db, user_id) == {}
    assert not (storage_dir / str(user_id)).exists()
    # The old session cookie no longer authenticates.
    client.cookies.clear()
    for name, value in old_cookies.items():
        client.cookies.set(name, value)
    assert client.get("/api/auth/me").status_code == 401


def test_data_93_api_delete_account_with_storage_down_still_ends_the_account(
    client: TestClient, db: Session, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, _, _ = _full_account(db)
    user_id = user.id
    _sign_in(client, db, user)
    old_cookies = dict(client.cookies.items())
    monkeypatch.setattr(storage, "delete_user_files", _failing_delete_user_files)

    response = client.delete("/api/account", headers=_xsrf(client))

    assert response.status_code == 200
    assert BACKUP_NOTICE in response.json()["message"]
    assert "storage unavailable" not in response.text
    client.cookies.clear()
    for name, value in old_cookies.items():
        client.cookies.set(name, value)
    assert client.get("/api/auth/me").status_code == 401
    assert len(_purge_jobs(db, user_id, JobStatus.QUEUED)) == 1

    monkeypatch.undo()
    purge_account(db, {"user_id": str(user_id)})
    db.commit()
    assert not _user_exists(db, user_id)
    assert not (storage_dir / str(user_id)).exists()


def test_api_delete_account_requires_csrf_header(client: TestClient, db: Session) -> None:
    user = _user(db)
    _sign_in(client, db, user)

    response = client.delete("/api/account")

    assert response.status_code == 403
    db.expire_all()
    stored = db.get(User, user.id)
    assert stored is not None
    assert stored.deletion_requested_at is None


def test_lac_45_api_delete_account_works_with_pending_terms(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    user, _, _ = _full_account(db)
    user.terms_version = "outdated-terms"
    user.privacy_version = "outdated-privacy"
    db.flush()
    user_id = user.id
    _sign_in(client, db, user)
    assert client.get("/api/resumes").status_code == 403  # TERMS_REQUIRED elsewhere

    response = client.delete("/api/account", headers=_xsrf(client))

    assert response.status_code == 200
    assert response.json() == {"message": ACCOUNT_DELETED_MESSAGE}
    assert _rows_referencing(db, user_id) == {}
    assert not (storage_dir / str(user_id)).exists()


def test_lac_45_api_delete_account_with_pending_terms_still_requires_csrf(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    user.terms_version = "outdated-terms"
    db.flush()
    _sign_in(client, db, user)

    response = client.delete("/api/account")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_FAILED"
    assert _user_exists(db, user.id)


def test_api_delete_account_requires_authentication(client: TestClient, db: Session) -> None:
    user = _user(db)
    db.commit()

    response = client.delete("/api/account")

    assert response.status_code == 401
    assert _user_exists(db, user.id)
