"""Integration tests for interview session deletion (TASK-062, CT-52).

Covers DATA-05 (the session and its messages, questions, answers, evaluations, report and
snapshot are deleted for good; pending jobs are closed), DATA-91 (a session deleted while
``evaluating`` never gets a report, including with the worker processing it and real commits)
and AUTH-16 at the HTTP layer.
"""

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from http.cookies import SimpleCookie
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from starlette.responses import Response as StarletteResponse

from app.auth.sessions import XSRF_COOKIE, XSRF_HEADER, create_auth_session
from app.config import get_settings
from app.db import get_db, get_sessionmaker
from app.errors import RESOURCE_NOT_FOUND, AppError
from app.evaluation import pipeline
from app.evaluation.evaluator import EVALUATION_TASK
from app.evaluation.pipeline import EVALUATE_JOB_KIND, run_evaluation
from app.interviews.question_generation import PREPARE_QUESTIONS_JOB
from app.jobs.queue import enqueue
from app.jobs.registry import JobRegistry
from app.jobs.worker import Worker
from app.main import create_app
from app.models.account import User
from app.models.assessment import Answer, Evaluation, Report
from app.models.interview import (
    InterviewSession,
    Message,
    MessageKind,
    MessageRole,
    Question,
    SessionStatus,
)
from app.models.job import Job, JobStatus
from app.privacy.session_deletion import delete_session
from tests.fakes.fake_llm import FakeLLM

SKILLS = ["Python", "PostgreSQL"]
SNAPSHOT = [
    {
        "id": "skill-1",
        "kind": "skill",
        "fields": {"name": "Python"},
        "origin": "explicit",
        "evidence": ["Python at Acme Corp"],
    }
]


@pytest.fixture(autouse=True)
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("IR_LLM_MAX_ATTEMPTS", "1")
    get_settings.cache_clear()
    yield
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


def _session(
    db: Session, user: User, status: SessionStatus = SessionStatus.EVALUATING
) -> InterviewSession:
    session = InterviewSession(
        user_id=user.id,
        status=status,
        resume_name="Jane Doe resume.pdf",
        requirement_items=[],
        proposal={"planned_count": len(SKILLS), "skills": SKILLS},
        planned_count=len(SKILLS),
        answered_count=len(SKILLS),
        snapshot=list(SNAPSHOT),
    )
    db.add(session)
    db.flush()
    for position, skill in enumerate(SKILLS, start=1):
        question = Question(
            session_id=session.id,
            position=position,
            skill_name=skill,
            text=f"Explain {skill}.",
            reference_points=[f"Point about {skill}"],
            sources=[],
            no_verified_source=True,
        )
        db.add(question)
        db.flush()
        db.add(
            Message(
                session_id=session.id,
                question_id=question.id,
                role=MessageRole.ASSISTANT,
                kind=MessageKind.INFO,
                content=f"Explain {skill}.",
            )
        )
        db.add(
            Answer(
                session_id=session.id,
                question_id=question.id,
                content=f"My answer about {skill}",
                idempotency_key=f"key-{position}",
            )
        )
    db.flush()
    return session


def _complete_with_report(db: Session, session: InterviewSession) -> None:
    answers = db.execute(select(Answer).where(Answer.session_id == session.id)).scalars().all()
    for answer in answers:
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
    session.status = SessionStatus.COMPLETED
    session.completed_at = datetime.now(UTC)
    db.flush()


def _evaluation(score: int = 4) -> dict[str, Any]:
    return {
        "score": score,
        "justification": f"Scored {score} for the content.",
        "evidence_quotes": [],
        "gap_explanation": None,
    }


def _count(db: Session, model: Any, session_id: uuid.UUID) -> int:
    db.expire_all()
    return int(
        db.execute(
            select(func.count()).select_from(model).where(model.session_id == session_id)
        ).scalar_one()
    )


def _evaluation_count(db: Session, session_id: uuid.UUID) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(Evaluation)
            .join(Answer, Answer.id == Evaluation.answer_id)
            .where(Answer.session_id == session_id)
        ).scalar_one()
    )


def _session_exists(db: Session, session_id: uuid.UUID) -> bool:
    db.expire_all()
    found = db.execute(
        select(InterviewSession.id).where(InterviewSession.id == session_id)
    ).scalar_one_or_none()
    return found is not None


def _job(db: Session, job_id: uuid.UUID) -> Job:
    db.expire_all()
    return db.execute(select(Job).where(Job.id == job_id)).scalar_one()


# --- DATA-05: definitive deletion ----------------------------------------------------------


def test_data_05_delete_removes_session_and_every_dependent_row(db: Session) -> None:
    user = _user(db)
    session = _session(db, user)
    _complete_with_report(db, session)
    session_id = session.id
    assert _evaluation_count(db, session_id) == len(SKILLS)

    delete_session(db, user, session_id)
    db.commit()

    assert not _session_exists(db, session_id)
    assert _count(db, Message, session_id) == 0
    assert _count(db, Question, session_id) == 0
    assert _count(db, Answer, session_id) == 0
    assert _evaluation_count(db, session_id) == 0
    assert _count(db, Report, session_id) == 0


def test_data_05_only_the_requested_session_is_deleted(db: Session) -> None:
    user = _user(db)
    kept = _session(db, user, SessionStatus.COMPLETED)
    deleted = _session(db, user, SessionStatus.CANCELLED)

    delete_session(db, user, deleted.id)
    db.commit()

    assert not _session_exists(db, deleted.id)
    assert _session_exists(db, kept.id)
    assert _count(db, Answer, kept.id) == len(SKILLS)
    assert _count(db, Message, kept.id) == len(SKILLS)


@pytest.mark.parametrize("status", list(SessionStatus))
def test_data_05_session_in_any_status_can_be_deleted(db: Session, status: SessionStatus) -> None:
    user = _user(db)
    session = _session(db, user, status)

    delete_session(db, user, session.id)
    db.commit()

    assert not _session_exists(db, session.id)


def test_data_05_rollback_keeps_the_session(db: Session) -> None:
    user = _user(db)
    session = _session(db, user)
    session_id = session.id
    db.commit()

    delete_session(db, user, session_id)
    db.rollback()

    assert _session_exists(db, session_id)
    assert _count(db, Answer, session_id) == len(SKILLS)


def test_auth_16_other_users_session_raises_not_found_and_deletes_nothing(db: Session) -> None:
    owner = _user(db)
    intruder = _user(db)
    session = _session(db, owner)
    job_id = enqueue(db, EVALUATE_JOB_KIND, {"session_id": str(session.id)})

    with pytest.raises(AppError) as raised:
        delete_session(db, intruder, session.id)

    assert raised.value.code == RESOURCE_NOT_FOUND
    assert _session_exists(db, session.id)
    assert _count(db, Answer, session.id) == len(SKILLS)
    assert _job(db, job_id).status == JobStatus.QUEUED


def test_auth_16_unknown_session_raises_not_found(db: Session) -> None:
    user = _user(db)

    with pytest.raises(AppError) as raised:
        delete_session(db, user, uuid.uuid4())

    assert raised.value.code == RESOURCE_NOT_FOUND


# --- DATA-05: pending jobs -----------------------------------------------------------------


def test_data_05_pending_jobs_of_the_session_are_marked_done(db: Session) -> None:
    user = _user(db)
    session = _session(db, user)
    other = _session(db, _user(db))
    evaluate_id = enqueue(db, EVALUATE_JOB_KIND, {"session_id": str(session.id)})
    prepare_id = enqueue(db, PREPARE_QUESTIONS_JOB, {"session_id": str(session.id)})
    other_id = enqueue(db, EVALUATE_JOB_KIND, {"session_id": str(other.id)})
    finished_id = enqueue(db, EVALUATE_JOB_KIND, {"session_id": str(session.id)})
    finished = _job(db, finished_id)
    finished.status = JobStatus.FAILED
    finished.last_error_code = "LLMUnavailable"
    db.flush()

    delete_session(db, user, session.id)
    db.commit()

    assert _job(db, evaluate_id).status == JobStatus.DONE
    assert _job(db, prepare_id).status == JobStatus.DONE
    assert _job(db, other_id).status == JobStatus.QUEUED
    assert _job(db, finished_id).status == JobStatus.FAILED


# --- DATA-91: deletion while evaluating ----------------------------------------------------


def test_data_91_deleted_while_evaluating_then_job_runs_creates_no_report(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM({EVALUATION_TASK: [_evaluation()] * len(SKILLS)})
    monkeypatch.setattr(pipeline, "get_llm_client", lambda: llm)
    user = _user(db)
    session = _session(db, user)
    session_id = session.id

    delete_session(db, user, session_id)
    db.commit()
    run_evaluation(db, {"session_id": str(session_id)})
    db.commit()

    assert _count(db, Report, session_id) == 0
    assert _evaluation_count(db, session_id) == 0
    assert not _session_exists(db, session_id)
    assert llm.calls == []


def test_data_91_deleted_during_inference_discards_the_result(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(db)
    session = _session(db, user)
    session_id = session.id

    class DeletingLLM(FakeLLM):
        def complete_structured(self, task, system, user_prompt, output_model):  # type: ignore[no-untyped-def,override]
            if not self.calls:
                # Same session as the job: flush only, so the job's loaded rows stay usable.
                # The real-commit race is covered by the worker test below.
                delete_session(db, user, session_id)
            return super().complete_structured(task, system, user_prompt, output_model)

    llm = DeletingLLM({EVALUATION_TASK: [_evaluation()] * len(SKILLS)})
    monkeypatch.setattr(pipeline, "get_llm_client", lambda: llm)

    run_evaluation(db, {"session_id": str(session_id)})
    db.commit()

    assert llm.calls
    assert _count(db, Report, session_id) == 0
    assert _evaluation_count(db, session_id) == 0
    assert not _session_exists(db, session_id)


@pytest.fixture
def committed_users(migrated_database: str) -> Iterator[list[uuid.UUID]]:
    user_ids: list[uuid.UUID] = []
    yield user_ids
    with get_sessionmaker()() as session:
        session.execute(delete(User).where(User.id.in_(user_ids)))
        session.commit()


@pytest.fixture
def committed_jobs(migrated_database: str) -> Iterator[list[uuid.UUID]]:
    job_ids: list[uuid.UUID] = []
    yield job_ids
    with get_sessionmaker()() as session:
        session.execute(delete(Job).where(Job.id.in_(job_ids)))
        session.commit()


def test_data_91_concurrent_deletion_while_worker_evaluates_with_real_commits(
    committed_users: list[uuid.UUID],
    committed_jobs: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with get_sessionmaker()() as setup:
        owner = _user(setup)
        session = _session(setup, owner)
        user_id, session_id = owner.id, session.id
        job_id = enqueue(setup, EVALUATE_JOB_KIND, {"session_id": str(session_id)})
        setup.commit()
    committed_users.append(user_id)
    committed_jobs.append(job_id)

    inference_started = threading.Event()
    deletion_done = threading.Event()
    errors: list[BaseException] = []

    class BlockingLLM(FakeLLM):
        def complete_structured(self, task, system, user_prompt, output_model):  # type: ignore[no-untyped-def,override]
            inference_started.set()
            assert deletion_done.wait(timeout=20)
            return super().complete_structured(task, system, user_prompt, output_model)

    llm = BlockingLLM({EVALUATION_TASK: [_evaluation()] * len(SKILLS)})
    monkeypatch.setattr(pipeline, "get_llm_client", lambda: llm)
    registry = JobRegistry()
    registry.register(EVALUATE_JOB_KIND, run_evaluation)
    worker = Worker(registry=registry, reap_every_seconds=None)
    worked: list[bool] = []

    def run_worker() -> None:
        try:
            worked.append(worker.run_once())
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(error)

    def run_deletion() -> None:
        try:
            assert inference_started.wait(timeout=20)
            with get_sessionmaker()() as db:
                deleter = db.get(User, user_id)
                assert deleter is not None
                delete_session(db, deleter, session_id)
                db.commit()
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(error)
        finally:
            deletion_done.set()

    threads = [threading.Thread(target=run_worker), threading.Thread(target=run_deletion)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert worked == [True]
    with get_sessionmaker()() as check:
        assert check.get(InterviewSession, session_id) is None
        assert _count(check, Report, session_id) == 0
        assert _count(check, Answer, session_id) == 0
        assert _evaluation_count(check, session_id) == 0
        job = check.get(Job, job_id)
        assert job is not None
        assert job.status == JobStatus.DONE


def test_data_05_concurrent_deletions_of_the_same_session_with_real_commits(
    committed_users: list[uuid.UUID],
) -> None:
    with get_sessionmaker()() as setup:
        owner = _user(setup)
        session = _session(setup, owner)
        user_id, session_id = owner.id, session.id
        setup.commit()
    committed_users.append(user_id)

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    errors: list[BaseException] = []

    def run_deletion() -> None:
        try:
            with get_sessionmaker()() as db:
                deleter = db.get(User, user_id)
                assert deleter is not None
                barrier.wait(timeout=10)
                try:
                    delete_session(db, deleter, session_id)
                    db.commit()
                    outcomes.append("deleted")
                except AppError as error:
                    db.rollback()
                    outcomes.append(error.code)
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(error)

    threads = [threading.Thread(target=run_deletion) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert sorted(outcomes) == sorted(["deleted", RESOURCE_NOT_FOUND])
    with get_sessionmaker()() as check:
        assert check.get(InterviewSession, session_id) is None


# --- HTTP: DELETE /api/sessions/{id} -------------------------------------------------------


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


def test_data_05_api_delete_own_session_returns_204(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _session(db, user)
    _complete_with_report(db, session)
    _sign_in(client, db, user)

    response = client.delete(f"/api/sessions/{session.id}", headers=_xsrf(client))

    assert response.status_code == 204
    assert response.content == b""
    assert not _session_exists(db, session.id)
    assert _count(db, Report, session.id) == 0


def test_auth_16_api_delete_other_users_session_returns_404_and_deletes_nothing(
    client: TestClient, db: Session
) -> None:
    owner = _user(db)
    intruder = _user(db)
    session = _session(db, owner)
    _sign_in(client, db, intruder)

    response = client.delete(f"/api/sessions/{session.id}", headers=_xsrf(client))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == RESOURCE_NOT_FOUND
    assert "Jane" not in response.text
    assert _session_exists(db, session.id)
    assert _count(db, Answer, session.id) == len(SKILLS)


def test_auth_16_api_delete_malformed_id_returns_404(client: TestClient, db: Session) -> None:
    user = _user(db)
    _sign_in(client, db, user)

    response = client.delete("/api/sessions/not-a-uuid", headers=_xsrf(client))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == RESOURCE_NOT_FOUND


def test_api_delete_session_requires_csrf_header(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _session(db, user)
    _sign_in(client, db, user)

    response = client.delete(f"/api/sessions/{session.id}")

    assert response.status_code == 403
    assert _session_exists(db, session.id)


def test_api_delete_session_requires_authentication(client: TestClient, db: Session) -> None:
    user = _user(db)
    session = _session(db, user)
    db.commit()

    response = client.delete(f"/api/sessions/{session.id}")

    assert response.status_code == 401
    assert _session_exists(db, session.id)
