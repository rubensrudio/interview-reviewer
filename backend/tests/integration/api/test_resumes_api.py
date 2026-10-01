"""Integration tests for the resume API routes (TASK-033, plan section 8.1 "Resumes").

Covers AUTH-16, CV-01, CV-02, CV-03, CV-04, CV-05, CV-07, CV-10, CV-11, CV-13, CV-90 and
CV-91 at the HTTP layer.
"""

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import Response as StarletteResponse

from app.api import resumes as resumes_api
from app.auth.sessions import XSRF_COOKIE, XSRF_HEADER, create_auth_session
from app.config import get_settings
from app.db import get_db
from app.main import create_app
from app.models.account import User
from app.models.job import Job
from app.models.resume import ExtractionItem, Resume, ResumeStatus

PDF_HEADER = b"%PDF-1.4\n"
MAX_BYTES = 5_242_880
NOT_FOUND_BODY = {"error": {"code": "RESOURCE_NOT_FOUND", "message": "Not found.", "details": None}}

NO_TEXT_MESSAGE = (
    "We couldn't read text from this PDF. Scanned documents are not supported yet — "
    "please upload a text-based PDF."
)
NOT_ENGLISH_MESSAGE = "Only resumes in English are supported at the moment."
PROCESSING_FAILED_MESSAGE = "We couldn't process this resume. Please try uploading it again later."


@pytest.fixture(autouse=True)
def storage_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "storage"
    monkeypatch.setenv("IR_STORAGE_DIR", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


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
    """Open a real session for ``user`` and load its cookies into ``client``."""
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


def _pdf(size: int = 1024) -> bytes:
    return PDF_HEADER + b"a" * (size - len(PDF_HEADER))


def _upload(
    client: TestClient,
    data: bytes,
    filename: str = "cv.pdf",
    headers: dict[str, str] | None = None,
) -> Response:
    return client.post(
        "/api/resumes",
        files={"file": (filename, data, "application/pdf")},
        headers=_xsrf(client) if headers is None else headers,
    )


def _send_json(client: TestClient, method: str, path: str, body: dict[str, Any]) -> Response:
    # json.dumps escapes lone surrogates (\\ud800), so hostile strings reach the server as JSON.
    return client.request(
        method,
        path,
        content=json.dumps(body),
        headers={"content-type": "application/json", **_xsrf(client)},
    )


def _explicit_item(item_id: str = "exp-1") -> dict[str, Any]:
    return ExtractionItem(
        id=item_id,
        kind="experience",
        fields={"title": "Backend Engineer", "organization": "Acme"},
        origin="explicit",
        evidence=["Backend Engineer at Acme"],
    ).model_dump(mode="json")


def _skill_item(item_id: str = "skill-1") -> dict[str, Any]:
    return ExtractionItem(
        id=item_id, kind="skill", fields={"name": "Python"}, origin="inferred", evidence=["Python"]
    ).model_dump(mode="json")


def _resume(
    db: Session,
    user: User,
    status: ResumeStatus = ResumeStatus.READY,
    extraction: list[dict[str, Any]] | None = None,
    failure_code: str | None = None,
    filename: str = "cv.pdf",
) -> Resume:
    resume = Resume(
        user_id=user.id,
        filename=filename,
        status=status,
        failure_code=failure_code,
        extraction=extraction,
    )
    db.add(resume)
    db.flush()
    return resume


def _ready_resume(db: Session, user: User, filename: str = "cv.pdf") -> Resume:
    return _resume(db, user, extraction=[_explicit_item(), _skill_item()], filename=filename)


def _stored_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()] if root.exists() else []


def _count_resumes(db: Session, user: User) -> int:
    return db.execute(
        select(func.count()).select_from(Resume).where(Resume.user_id == user.id)
    ).scalar_one()


def _count_jobs(db: Session) -> int:
    return db.execute(
        select(func.count()).select_from(Job).where(Job.kind == "resume.process")
    ).scalar_one()


def _stored_items(db: Session, resume: Resume) -> list[dict[str, Any]]:
    db.expire_all()
    refreshed = db.get(Resume, resume.id)
    assert refreshed is not None
    return refreshed.extraction or []


# --- AUTH-16: isolation between users --------------------------------------------------------


def test_auth_16_other_user_gets_uniform_404_without_resource_body(
    client: TestClient, db: Session
) -> None:
    owner = _user(db)
    resume = _ready_resume(db, owner, filename="owner-secret-cv.pdf")
    _sign_in(client, db, _user(db))
    base = f"/api/resumes/{resume.id}"

    responses = [
        client.get(base),
        _send_json(client, "POST", f"{base}/items", {"kind": "skill", "fields": {"name": "Go"}}),
        _send_json(client, "PATCH", f"{base}/items/exp-1", {"fields": {"title": "CTO"}}),
        client.delete(f"{base}/items/exp-1", headers=_xsrf(client)),
    ]

    for response in responses:
        assert response.status_code == 404
        assert response.json() == NOT_FOUND_BODY
        assert "owner-secret-cv" not in response.text
    assert _stored_items(db, resume) == [_explicit_item(), _skill_item()]


def test_auth_16_unknown_and_malformed_ids_get_the_same_404(
    client: TestClient, db: Session
) -> None:
    _sign_in(client, db, _user(db))

    for resume_id in (str(uuid.uuid4()), "not-a-uuid", "%00", "%ED%A0%80"):
        response = client.get(f"/api/resumes/{resume_id}")
        assert response.status_code == 404
        assert response.json() == NOT_FOUND_BODY


def test_auth_16_routes_require_a_session(client: TestClient, db: Session) -> None:
    resume = _ready_resume(db, _user(db))

    assert client.get("/api/resumes").status_code == 401
    assert client.get(f"/api/resumes/{resume.id}").status_code == 401
    assert client.post("/api/resumes", files={"file": ("cv.pdf", _pdf())}).status_code == 401


# --- CV-13 / CV-11 / CV-05: listing ----------------------------------------------------------


def test_cv_13_list_shows_only_own_versions_with_name_date_and_status(
    client: TestClient, db: Session
) -> None:
    user_a, user_b = _user(db), _user(db)
    own = _resume(db, user_a, status=ResumeStatus.RECEIVED, filename="mine.pdf")
    _resume(db, user_b, status=ResumeStatus.READY, filename="theirs.pdf")
    _sign_in(client, db, user_a)

    response = client.get("/api/resumes")

    assert response.status_code == 200
    body = response.json()
    assert [entry["id"] for entry in body] == [str(own.id)]
    assert body[0]["filename"] == "mine.pdf"
    assert body[0]["status"] == "received"
    assert body[0]["uploaded_at"]
    assert body[0]["failure_code"] is None
    assert body[0]["failure_message"] is None
    assert "theirs.pdf" not in response.text


def test_cv_11_status_filter_offers_only_ready_versions(client: TestClient, db: Session) -> None:
    user = _user(db)
    ready = _ready_resume(db, user)
    _resume(db, user, status=ResumeStatus.PROCESSING)
    _resume(db, user, status=ResumeStatus.FAILED, failure_code="NO_TEXT")
    _sign_in(client, db, user)

    response = client.get("/api/resumes", params={"status": "ready"})

    assert response.status_code == 200
    assert [entry["id"] for entry in response.json()] == [str(ready.id)]


@pytest.mark.parametrize("status", ["unknown", "READY", "", "\x00"])
def test_cv_11_invalid_status_filter_is_validation_error(
    client: TestClient, db: Session, status: str
) -> None:
    _sign_in(client, db, _user(db))

    response = client.get("/api/resumes", params={"status": status})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_cv_05_list_reflects_the_current_status_without_new_upload(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    resume = _resume(db, user, status=ResumeStatus.RECEIVED)
    _sign_in(client, db, user)
    assert client.get("/api/resumes").json()[0]["status"] == "received"

    resume.status = ResumeStatus.PROCESSING
    db.commit()

    assert client.get("/api/resumes").json()[0]["status"] == "processing"


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("NO_TEXT", NO_TEXT_MESSAGE),
        ("OCR_NO_TEXT", NO_TEXT_MESSAGE),
        ("NOT_ENGLISH", NOT_ENGLISH_MESSAGE),
        ("CORRUPTED", PROCESSING_FAILED_MESSAGE),
        ("PROTECTED", PROCESSING_FAILED_MESSAGE),
        ("EXTRACTION_INVALID", PROCESSING_FAILED_MESSAGE),
        ("LLM_UNAVAILABLE", PROCESSING_FAILED_MESSAGE),
        ("SOMETHING_NEW", PROCESSING_FAILED_MESSAGE),
        (None, PROCESSING_FAILED_MESSAGE),
    ],
)
def test_cv_05_failed_version_carries_the_section_9_message(
    client: TestClient, db: Session, code: str | None, message: str
) -> None:
    user = _user(db)
    _resume(db, user, status=ResumeStatus.FAILED, failure_code=code)
    _sign_in(client, db, user)

    entry = client.get("/api/resumes").json()[0]

    assert entry["status"] == "failed"
    assert entry["failure_code"] == code
    assert entry["failure_message"] == message


# --- CV-07: detail ---------------------------------------------------------------------------


def test_cv_07_ready_version_returns_items_with_origin_and_evidence(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    resume = _ready_resume(db, user)
    _sign_in(client, db, user)

    response = client.get(f"/api/resumes/{resume.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(resume.id)
    assert body["status"] == "ready"
    assert body["items"] == [_explicit_item(), _skill_item()]
    for item in body["items"]:
        assert item["origin"] in {"explicit", "inferred"}
        assert item["evidence"]


@pytest.mark.parametrize("status", [ResumeStatus.RECEIVED, ResumeStatus.PROCESSING])
def test_cv_07_version_not_ready_has_no_items(
    client: TestClient, db: Session, status: ResumeStatus
) -> None:
    user = _user(db)
    resume = _resume(db, user, status=status)
    _sign_in(client, db, user)

    body = client.get(f"/api/resumes/{resume.id}").json()

    assert body["status"] == status.value
    assert body["items"] is None


def test_cv_07_ready_version_without_extraction_has_empty_items(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    resume = _resume(db, user, extraction=None)
    _sign_in(client, db, user)

    assert client.get(f"/api/resumes/{resume.id}").json()["items"] == []


# --- CV-01 / CV-02 / CV-03 / CV-04 / CV-90 / CV-91: upload ---------------------------------


def test_cv_01_upload_stores_a_received_version(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    user = _user(db)
    _sign_in(client, db, user)
    jobs_before = _count_jobs(db)

    response = _upload(client, _pdf(), filename="my resume.pdf")

    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "my resume.pdf"
    assert body["status"] == "received"
    assert body["uploaded_at"]
    assert body["failure_code"] is None
    assert body["failure_message"] is None
    assert set(body) == {
        "id",
        "filename",
        "uploaded_at",
        "status",
        "failure_code",
        "failure_message",
    }
    assert _count_resumes(db, user) == 1
    assert _count_jobs(db) == jobs_before + 1
    assert len(_stored_files(storage_dir)) == 1


def test_cv_01_upload_requires_the_csrf_header_and_stores_nothing(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    user = _user(db)
    _sign_in(client, db, user)

    response = _upload(client, _pdf(), headers={})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_FAILED"
    assert _count_resumes(db, user) == 0
    assert _stored_files(storage_dir) == []


def test_cv_03_upload_of_6_mb_is_rejected_with_the_limit(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    user = _user(db)
    _sign_in(client, db, user)

    response = _upload(client, _pdf(6 * 1024 * 1024))

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "FILE_TOO_LARGE"
    assert error["details"]["limit_bytes"] == 5242880
    assert _count_resumes(db, user) == 0
    assert _stored_files(storage_dir) == []


def test_cv_91_exactly_the_limit_is_accepted(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    user = _user(db)
    _sign_in(client, db, user)

    response = _upload(client, _pdf(MAX_BYTES))

    assert response.status_code == 201
    assert _stored_files(storage_dir)[0].stat().st_size == MAX_BYTES


def test_cv_91_one_byte_above_the_limit_is_rejected(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    user = _user(db)
    _sign_in(client, db, user)

    response = _upload(client, _pdf(MAX_BYTES + 1))

    assert response.status_code == 413
    assert response.json()["error"]["details"] == {"limit_bytes": MAX_BYTES}
    assert _count_resumes(db, user) == 0
    assert _stored_files(storage_dir) == []


def test_cv_03_body_above_the_cap_is_rejected_before_parsing(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    user = _user(db)
    _sign_in(client, db, user)
    oversized = b"x" * (MAX_BYTES + resumes_api.MULTIPART_OVERHEAD_BYTES + 1)

    response = client.post(
        "/api/resumes",
        content=oversized,
        headers={"content-type": "multipart/form-data; boundary=abc", **_xsrf(client)},
    )

    assert response.status_code == 413
    assert response.json()["error"]["details"] == {"limit_bytes": MAX_BYTES}
    assert _stored_files(storage_dir) == []


def test_cv_03_chunked_body_above_the_cap_is_rejected(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    user = _user(db)
    _sign_in(client, db, user)
    chunk = b"x" * (1024 * 1024)

    def chunks() -> Iterator[bytes]:
        for _ in range(7):
            yield chunk

    response = client.post(
        "/api/resumes",
        content=chunks(),
        headers={"content-type": "multipart/form-data; boundary=abc", **_xsrf(client)},
    )

    assert response.status_code == 413
    assert _stored_files(storage_dir) == []


def test_cv_02_non_pdf_content_is_rejected_whatever_the_extension(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    user = _user(db)
    _sign_in(client, db, user)

    response = _upload(client, b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, filename="cv.pdf")

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "INVALID_PDF"
    assert _count_resumes(db, user) == 0
    assert _stored_files(storage_dir) == []


def test_cv_90_empty_file_is_invalid(client: TestClient, db: Session, storage_dir: Path) -> None:
    user = _user(db)
    _sign_in(client, db, user)

    response = _upload(client, b"")

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "INVALID_PDF"
    assert _stored_files(storage_dir) == []


def test_cv_04_eleventh_version_is_rejected(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    user = _user(db)
    for _ in range(10):
        _resume(db, user, status=ResumeStatus.FAILED, failure_code="NO_TEXT")
    _sign_in(client, db, user)

    response = _upload(client, _pdf())

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "RESUME_LIMIT_REACHED"
    assert "Delete one" in error["message"]
    assert _count_resumes(db, user) == 10
    assert _stored_files(storage_dir) == []


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"", "multipart/form-data; boundary=abc"),
        (b"file=abc", "application/x-www-form-urlencoded"),
        (b'{"file": "abc"}', "application/json"),
        (b"--abc\r\nbroken", "multipart/form-data; boundary=abc"),
    ],
)
def test_cv_01_request_without_a_file_part_is_validation_error(
    client: TestClient, db: Session, storage_dir: Path, content: bytes, content_type: str
) -> None:
    _sign_in(client, db, _user(db))

    response = client.post(
        "/api/resumes", content=content, headers={"content-type": content_type, **_xsrf(client)}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _stored_files(storage_dir) == []


def test_cv_01_more_than_one_file_is_validation_error(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    _sign_in(client, db, _user(db))

    response = client.post(
        "/api/resumes",
        files=[("file", ("a.pdf", _pdf())), ("file", ("b.pdf", _pdf()))],
        headers=_xsrf(client),
    )

    assert response.status_code == 422
    assert _stored_files(storage_dir) == []


@pytest.mark.parametrize("filename", ["cv\x00.pdf", "../../etc/passwd", "..\\..\\cv.pdf"])
def test_cv_01_hostile_filenames_never_cause_a_server_error(
    client: TestClient, db: Session, filename: str
) -> None:
    _sign_in(client, db, _user(db))

    response = _upload(client, _pdf(), filename=filename)

    assert response.status_code == 201
    stored = response.json()["filename"]
    assert "\x00" not in stored
    assert "/" not in stored


def test_cv_01_part_without_filename_is_not_a_file(
    client: TestClient, db: Session, storage_dir: Path
) -> None:
    # Multipart treats a part with an empty filename as a plain field, not as a file.
    _sign_in(client, db, _user(db))

    response = _upload(client, _pdf(), filename="")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _stored_files(storage_dir) == []


def test_cv_01_failed_commit_removes_the_stored_file(
    client: TestClient, db: Session, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(db)
    _sign_in(client, db, user)

    def failing_commit() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db, "commit", failing_commit)

    response = _upload(client, _pdf())

    assert response.status_code == 500
    assert "commit failed" not in response.text
    assert _stored_files(storage_dir) == []


# --- CV-10: editing --------------------------------------------------------------------------


def test_cv_10_add_item_returns_201_user_provided_without_evidence(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    resume = _ready_resume(db, user)
    _sign_in(client, db, user)

    response = _send_json(
        client,
        "POST",
        f"/api/resumes/{resume.id}/items",
        {"kind": "skill", "fields": {"name": "Go"}},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "skill"
    assert body["fields"] == {"name": "Go"}
    assert body["origin"] == "user_provided"
    assert body["evidence"] == []
    assert _stored_items(db, resume)[-1] == body


def test_cv_10_update_item_returns_200_user_provided_without_evidence(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    resume = _ready_resume(db, user)
    _sign_in(client, db, user)

    response = _send_json(
        client, "PATCH", f"/api/resumes/{resume.id}/items/exp-1", {"fields": {"title": "CTO"}}
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "id": "exp-1",
        "kind": "experience",
        "fields": {"title": "CTO"},
        "origin": "user_provided",
        "evidence": [],
    }
    assert _stored_items(db, resume)[0] == body


def test_cv_10_delete_item_returns_204_and_removes_it(client: TestClient, db: Session) -> None:
    user = _user(db)
    resume = _ready_resume(db, user)
    _sign_in(client, db, user)

    response = client.delete(f"/api/resumes/{resume.id}/items/exp-1", headers=_xsrf(client))

    assert response.status_code == 204
    assert response.content == b""
    assert _stored_items(db, resume) == [_skill_item()]


def test_cv_10_editing_a_version_not_ready_is_409(client: TestClient, db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user, status=ResumeStatus.PROCESSING)
    _sign_in(client, db, user)

    response = _send_json(
        client,
        "POST",
        f"/api/resumes/{resume.id}/items",
        {"kind": "skill", "fields": {"name": "Go"}},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RESUME_NOT_READY"


def test_cv_10_editing_requires_the_csrf_header(client: TestClient, db: Session) -> None:
    user = _user(db)
    resume = _ready_resume(db, user)
    _sign_in(client, db, user)

    response = client.delete(f"/api/resumes/{resume.id}/items/exp-1")

    assert response.status_code == 403
    assert _stored_items(db, resume) == [_explicit_item(), _skill_item()]


@pytest.mark.parametrize("item_id", ["unknown", "%00", "%ED%A0%80", "x" * 65])
def test_cv_10_unknown_or_hostile_item_id_is_404(
    client: TestClient, db: Session, item_id: str
) -> None:
    user = _user(db)
    resume = _ready_resume(db, user)
    _sign_in(client, db, user)

    patch = _send_json(
        client, "PATCH", f"/api/resumes/{resume.id}/items/{item_id}", {"fields": {"name": "Go"}}
    )
    delete = client.delete(f"/api/resumes/{resume.id}/items/{item_id}", headers=_xsrf(client))

    assert patch.status_code == 404
    assert delete.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "hobby", "fields": {"name": "Go"}},
        {"fields": {"name": "Go"}},
        {"kind": "skill", "fields": {"name": 1}},
        {"kind": "skill", "fields": {"name": "\ud800"}},
        {"kind": "skill", "fields": {"name": "a\x00b"}},
        {"kind": "skill", "fields": {"\ud800": "Go"}},
        {"kind": "skill", "fields": {"unknown": "Go"}},
        {"kind": "skill", "fields": {}},
        {"kind": "skill", "fields": ["Go"]},
        {"kind": "skill", "fields": {"name": "Go"}, "origin": "explicit"},
    ],
)
def test_cv_10_invalid_item_body_is_validation_error(
    client: TestClient, db: Session, body: dict[str, Any]
) -> None:
    user = _user(db)
    resume = _ready_resume(db, user)
    _sign_in(client, db, user)

    response = _send_json(client, "POST", f"/api/resumes/{resume.id}/items", body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _stored_items(db, resume) == [_explicit_item(), _skill_item()]


def test_cv_10_item_body_above_16_kib_is_rejected(client: TestClient, db: Session) -> None:
    user = _user(db)
    resume = _ready_resume(db, user)
    _sign_in(client, db, user)

    response = _send_json(
        client,
        "POST",
        f"/api/resumes/{resume.id}/items",
        {"kind": "skill", "fields": {"name": "Go", "description": "x" * (17 * 1024)}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
