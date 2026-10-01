"""System test of the full flow with outbound network blocked (TASK-068; KNOW-01, KNOW-02).

The candidate journey runs end to end through the HTTP API and the real job worker:
resume upload -> resume processing job -> session start -> job requirements -> list and plan
confirmation -> question preparation job -> answers -> evaluation job -> report.

Every inference goes through the scripted ``FakeLLM`` (the stand-in for the private LLM server
of DA-2), and ``pytest-socket`` blocks every Python socket connection except the local
PostgreSQL host. The flow must end with the session ``completed`` and a stored report, which
shows it never needs the internet (KNOW-02) nor an external LLM provider (KNOW-01).

The worker and the API commit for real, so the test removes its user (cascading to resumes,
sessions and reports) and its jobs at teardown.
"""

import io
import shutil
import socket
import subprocess  # noqa: S404 - drives the docker CLI with fixed arguments
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pytest_socket import SocketConnectBlockedError, socket_allow_hosts
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import delete, make_url, or_, select
from sqlalchemy.orm import Session
from starlette.responses import Response as StarletteResponse

from app.api.session_requirements import get_llm_factory
from app.auth.sessions import XSRF_COOKIE, XSRF_HEADER, create_auth_session
from app.config import get_settings
from app.db import get_sessionmaker
from app.evaluation import pipeline
from app.evaluation.evaluator import EVALUATION_TASK
from app.evaluation.pipeline import EVALUATE_JOB_KIND, run_evaluation
from app.evaluation.reference_answers import REFERENCE_ANSWER_TASK
from app.interviews import question_generation
from app.interviews.question_generation import (
    PREPARE_QUESTIONS_JOB,
    QUESTION_GENERATION_TASK,
    prepare_questions,
)
from app.interviews.requirements import REQUIREMENTS_TASK
from app.jobs.registry import JobRegistry
from app.jobs.worker import Worker
from app.llm.client import LLMClient
from app.main import create_app
from app.models.account import User
from app.models.assessment import Report
from app.models.interview import InterviewSession, SessionStatus
from app.models.job import Job, JobStatus
from app.resumes import processing
from app.resumes.extraction import EXTRACTION_TASK
from app.resumes.processing import RESUME_PROCESS_JOB, process_resume
from tests.fakes.fake_llm import FakeLLM

REPO_ROOT = Path(__file__).resolve().parents[3]
PROD_COMPOSE = REPO_ROOT / "deploy" / "docker-compose.prod.yml"
PROXY_SERVICE = "egress-proxy"
PROXY_SETTLE_SECONDS = 8
PROXY_STARTUP_TIMEOUT_SECONDS = 60
CURL_IMAGE = "curlimages/curl:latest"

# TEST-NET-3 (RFC 5737): never routed, so the probe cannot leave the machine even unblocked.
EXTERNAL_PROBE = ("203.0.113.10", 443)
MAX_WORKER_ROUNDS = 20

RESUME_LINES = [
    "Riley Placeholder - Senior Backend Engineer",
    "Experience: Backend Engineer at Northwind Logistics from 2019 to 2023.",
    "Built order routing services in Python and FastAPI backed by PostgreSQL.",
    "Designed REST APIs consumed by thousands of warehouses every single day.",
    "Education: BSc Computer Science at Lakeside State University, 2015 - 2019.",
    "Skills: Python, FastAPI, PostgreSQL, Docker, Kubernetes, testing and CI/CD.",
]

REQUIREMENTS_TEXT = (
    "We are hiring a backend engineer to build and operate our payment platform.\n"
    "Required: strong experience with Python and PostgreSQL in production.\n"
    "Good communication skills and 5+ years of professional experience are expected."
)

SKILLS = ["Python", "PostgreSQL"]

EXTRACTION = {
    "items": [
        {
            "kind": "experience",
            "fields": {
                "title": "Backend Engineer",
                "organization": "Northwind Logistics",
                "start": "2019",
                "end": "2023",
            },
            "origin": "explicit",
            "evidence": ["Backend Engineer at Northwind Logistics from 2019 to 2023."],
        },
        {
            "kind": "skill",
            "fields": {"name": "Python"},
            "origin": "explicit",
            "evidence": ["Skills: Python, FastAPI, PostgreSQL"],
        },
    ]
}

REQUIREMENTS = {
    "items": [
        {
            "name": skill,
            "original_terms": [skill],
            "classification": "required",
            "level": None,
            "ambiguous": False,
            "clarification_question": None,
        }
        for skill in SKILLS
    ],
    "non_technical": ["Good communication"],
}

QUESTIONS = {
    "questions": [
        {
            "skill": "Python",
            "text": "How does the GIL affect CPU-bound Python services?",
            "reference_points": ["Only one thread runs bytecode at a time"],
        },
        {
            "skill": "PostgreSQL",
            "text": "When would you add a partial index in PostgreSQL?",
            "reference_points": ["Index only the rows matched by a predicate"],
        },
    ]
}


def _evaluation(score: int) -> dict[str, Any]:
    return {
        "score": score,
        "justification": f"Scored {score} for the content.",
        "evidence_quotes": [],
        "gap_explanation": None if score >= 3 else "Missing key points.",
    }


REFERENCE = {
    "text": "A partial index covers only the rows that match its WHERE clause.",
    "points": ["Index only the rows matched by a predicate"],
    "source_urls": [],
    "example_text": None,
    "example_evidence": None,
}


def _scripted_llm() -> FakeLLM:
    return FakeLLM(
        {
            EXTRACTION_TASK: [EXTRACTION],
            REQUIREMENTS_TASK: [REQUIREMENTS],
            QUESTION_GENERATION_TASK: [QUESTIONS],
            EVALUATION_TASK: [_evaluation(4), _evaluation(2)],
            REFERENCE_ANSWER_TASK: [REFERENCE],
        }
    )


def _make_pdf(lines: list[str]) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    y = 800
    for line in lines:
        pdf.drawString(50, y, line)
        y -= 20
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


@pytest.fixture
def settings_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("IR_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("IR_LLM_MAX_ATTEMPTS", "1")
    monkeypatch.delenv("IR_OCR_ENABLED", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """Route every inference of the flow to one scripted fake LLM."""
    fake = _scripted_llm()

    def factory() -> LLMClient:
        return fake

    monkeypatch.setattr(processing, "get_llm_client", factory)
    monkeypatch.setattr(question_generation, "get_llm_client", factory)
    monkeypatch.setattr(pipeline, "get_llm_client", factory)
    return fake


@pytest.fixture
def committed_user(migrated_database: str, settings_env: None) -> Iterator[uuid.UUID]:
    settings = get_settings()
    now = datetime.now(UTC)
    with get_sessionmaker()() as db:
        user = User(
            email_normalized=f"offline-{uuid.uuid4().hex}@example.com",
            email_verified_at=now,
            terms_version=settings.terms_version,
            privacy_version=settings.privacy_version,
            terms_accepted_at=now,
        )
        db.add(user)
        db.commit()
        user_id = user.id
    yield user_id
    with get_sessionmaker()() as db:
        db.execute(delete(User).where(User.id == user_id))
        db.commit()


@pytest.fixture
def tracked_ids(migrated_database: str) -> Iterator[list[str]]:
    """Resume and session ids of the test; their jobs are deleted at teardown."""
    ids: list[str] = []
    yield ids
    if not ids:
        return
    with get_sessionmaker()() as db:
        db.execute(
            delete(Job).where(
                or_(
                    Job.payload["resume_id"].astext.in_(ids),
                    Job.payload["session_id"].astext.in_(ids),
                )
            )
        )
        db.commit()


@pytest.fixture
def network_blocked(migrated_database: str) -> Iterator[str]:
    """Block every Python socket connection except the local PostgreSQL host (KNOW-02)."""
    db_host = make_url(migrated_database).host or "localhost"
    socket_allow_hosts([db_host], allow_unix_socket=True)
    yield db_host
    # pytest-socket lifts the restriction itself in its runtest teardown hook.


@pytest.fixture
def client(llm: FakeLLM, network_blocked: str) -> Iterator[TestClient]:
    app = create_app()

    def override_llm_factory() -> Callable[[], LLMClient]:
        return lambda: llm

    app.dependency_overrides[get_llm_factory] = override_llm_factory
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _sign_in(client: TestClient, user_id: uuid.UUID) -> None:
    response = StarletteResponse()
    with get_sessionmaker()() as db:
        user = db.get(User, user_id)
        assert user is not None
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


def _post(client: TestClient, path: str, body: Any = None, **headers: str) -> Response:
    return client.post(path, json=body, headers={**_xsrf(client), **headers})


def _worker() -> Worker:
    registry = JobRegistry()
    registry.register(RESUME_PROCESS_JOB, process_resume)
    registry.register(PREPARE_QUESTIONS_JOB, prepare_questions)
    registry.register(EVALUATE_JOB_KIND, run_evaluation)
    # Reaper disabled so the test never touches jobs of other tests.
    return Worker(registry=registry, reap_every_seconds=None)


def _job_statuses(kind: str, key: str, value: str) -> list[JobStatus]:
    with get_sessionmaker()() as db:
        rows = db.execute(
            select(Job.status).where(Job.kind == kind, Job.payload[key].astext == value)
        ).scalars()
        return list(rows)


def _drain(worker: Worker, kind: str, key: str, value: str) -> None:
    """Run the worker until the job of ``kind`` for ``key=value`` is no longer pending."""
    for _ in range(MAX_WORKER_ROUNDS):
        statuses = _job_statuses(kind, key, value)
        if statuses and all(s in (JobStatus.DONE, JobStatus.FAILED) for s in statuses):
            assert statuses == [JobStatus.DONE]
            return
        worker.run_once()
    raise AssertionError(f"job {kind} was not processed")


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_know_02_external_connections_are_blocked(network_blocked: str) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(SocketConnectBlockedError):
            probe.connect(EXTERNAL_PROBE)
    finally:
        probe.close()


def test_know_01_know_02_full_flow_completes_offline_with_private_llm(
    client: TestClient,
    llm: FakeLLM,
    committed_user: uuid.UUID,
    tracked_ids: list[str],
) -> None:
    _sign_in(client, committed_user)
    worker = _worker()

    # Resume upload and processing job.
    upload = client.post(
        "/api/resumes",
        files={"file": ("cv.pdf", _make_pdf(RESUME_LINES), "application/pdf")},
        headers=_xsrf(client),
    )
    assert upload.status_code == 201, upload.text
    resume_id = upload.json()["id"]
    tracked_ids.append(resume_id)
    _drain(worker, RESUME_PROCESS_JOB, "resume_id", resume_id)
    resume = client.get(f"/api/resumes/{resume_id}")
    assert resume.json()["status"] == "ready", resume.text

    # Session start, job requirements, list and plan confirmation.
    started = _post(
        client,
        "/api/sessions",
        {"resume_id": resume_id, "language": "en", "interview_level": "mid-level"},
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["id"]
    tracked_ids.append(session_id)

    structured = _post(
        client, f"/api/sessions/{session_id}/requirements", {"text": REQUIREMENTS_TEXT}
    )
    assert structured.status_code == 200, structured.text
    assert structured.json()["status"] == "awaiting_confirmation"

    confirmed_list = _post(client, f"/api/sessions/{session_id}/requirement-list/confirm")
    assert confirmed_list.status_code == 200, confirmed_list.text
    confirmed_plan = _post(client, f"/api/sessions/{session_id}/plan/confirm")
    assert confirmed_plan.status_code == 200, confirmed_plan.text
    assert confirmed_plan.json()["status"] == "preparing_questions"

    # Question preparation job, then one answer per planned question.
    _drain(worker, PREPARE_QUESTIONS_JOB, "session_id", session_id)
    view = client.get(f"/api/sessions/{session_id}").json()
    assert view["status"] == "in_interview"
    for position in range(1, len(SKILLS) + 1):
        question = view["current_question"]
        assert question is not None and question["position"] == position
        answered = _post(
            client,
            f"/api/sessions/{session_id}/answers",
            {"question_id": question["id"], "content": f"My answer about {question['skill']}."},
            **{"Idempotency-Key": f"offline-{position}"},
        )
        assert answered.status_code == 200, answered.text
        view = answered.json()
    assert view["status"] == "evaluating"

    # Evaluation job and report.
    _drain(worker, EVALUATE_JOB_KIND, "session_id", session_id)
    final = client.get(f"/api/sessions/{session_id}").json()
    assert final["status"] == "completed"
    assert final["report_available"] is True
    report = client.get(f"/api/sessions/{session_id}/report")
    assert report.status_code == 200, report.text

    with get_sessionmaker()() as db:
        stored = db.execute(
            select(InterviewSession.status).where(InterviewSession.id == uuid.UUID(session_id))
        ).scalar_one()
        assert stored == SessionStatus.COMPLETED
        assert _report_count(db, session_id) == 1

    # KNOW-01: every inference of the flow went to the private LLM client, none left over.
    for task in (
        EXTRACTION_TASK,
        REQUIREMENTS_TASK,
        QUESTION_GENERATION_TASK,
        EVALUATION_TASK,
        REFERENCE_ANSWER_TASK,
    ):
        assert llm.calls_for(task), task
        assert llm.remaining(task) == 0, task


def _report_count(db: Session, session_id: str) -> int:
    rows = db.execute(select(Report.id).where(Report.session_id == uuid.UUID(session_id)))
    return len(rows.all())


# --- egress proxy of the production compose (KNOW-02) ---------------------------------------


def _docker_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    probe = subprocess.run(  # noqa: S603 - fixed arguments
        [docker, "info"], capture_output=True, check=False, timeout=30
    )
    return probe.returncode == 0


def _docker(*args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    assert docker is not None
    return subprocess.run(  # noqa: S603 - fixed arguments
        [docker, *args], capture_output=True, text=True, check=False, timeout=timeout
    )


@pytest.fixture
def egress_proxy() -> Iterator[tuple[str, str]]:
    """Start only the egress proxy of the production compose; yield (container, network)."""
    if not _docker_available():
        pytest.skip("docker is not available")
    project = f"ir-egress-{uuid.uuid4().hex[:8]}"
    compose = ["compose", "-p", project, "-f", str(PROD_COMPOSE)]
    try:
        started = _docker(*compose, "up", "-d", PROXY_SERVICE, timeout=600)
        assert started.returncode == 0, started.stderr
        container = _docker(*compose, "ps", "-a", "-q", PROXY_SERVICE).stdout.strip()
        assert container, "egress proxy container was not created"
        yield container, f"{project}_internal"
    finally:
        _docker(*compose, "down", "-v", "--remove-orphans")


def _proxy_state(container: str) -> tuple[str, int]:
    inspected = _docker(
        "inspect", "--format", "{{.State.Status}} {{.RestartCount}}", container
    ).stdout.split()
    assert len(inspected) == 2, inspected
    return inspected[0], int(inspected[1])


def _wait_until_listening(network: str) -> None:
    deadline = time.monotonic() + PROXY_STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _connect_status(network, "example.com:443") != "000":
            return
        time.sleep(1)
    raise AssertionError("egress proxy never answered on port 3128")


def _connect_status(network: str, target: str) -> str:
    """Status code of ``CONNECT target`` sent to the proxy from the internal network."""
    result = _docker(
        "run", "--rm", "--network", network, CURL_IMAGE,
        "-s", "-o", "/dev/null", "--max-time", "15", "-p",
        "-x", f"http://{PROXY_SERVICE}:3128", "-w", "%{http_connect}",
        f"https://{target}/",
    )  # fmt: skip
    return result.stdout.strip()[-3:] or "000"


def test_know_02_egress_proxy_stays_running_and_applies_allowlist(
    egress_proxy: tuple[str, str],
) -> None:
    container, network = egress_proxy
    _wait_until_listening(network)
    time.sleep(PROXY_SETTLE_SECONDS)

    status, restarts = _proxy_state(container)
    logs = _docker("logs", container).stderr[-2000:]
    assert (status, restarts) == ("running", 0), logs

    # Denied: anything outside the allowlist, including LLM providers.
    for target in ("example.com:443", "api.openai.com:443", "accounts.google.com:80"):
        assert _connect_status(network, target) == "403", target
    # Allowed: the proxy never denies Google sign-in (200 online, 503 without internet).
    for target in ("accounts.google.com:443", "oauth2.googleapis.com:443"):
        assert _connect_status(network, target) != "403", target
