"""Integration tests for the local authentication API routes (TASK-019, plan section 8.1).

Covers AUTH-01, AUTH-02, AUTH-03, AUTH-04, AUTH-05, AUTH-06, AUTH-07, AUTH-08, AUTH-14,
AUTH-17, AUTH-90, AUTH-92, AUTH-94 and AUTH-95 at the HTTP layer, plus the request size
limits of LAC-32 and the verification message of LAC-31.
"""

import json
import re
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import auth_local
from app.auth import password_reset, registration
from app.auth.passwords import hash_password
from app.auth.sessions import SESSION_COOKIE, XSRF_COOKIE, XSRF_HEADER
from app.auth.tokens import hash_secret
from app.config import get_settings
from app.db import get_db
from app.email.templates import EmailContent
from app.main import create_app
from app.models.account import AuthSession, User

GOOD_PASSWORD = "correct horse battery staple 42"  # noqa: S105 - test fixture value
NEW_PASSWORD = "another horse battery staple 43"  # noqa: S105 - test fixture value
WRONG_PASSWORD = "wrong horse battery staple 44"  # noqa: S105 - test fixture value

REGISTER_MESSAGE = (
    "Check your inbox to continue. If you already have an account, sign in or reset your password."
)
FORGOT_MESSAGE = (
    "If an account exists for this e-mail, we sent instructions to reset your password."
)
VERIFIED_MESSAGE = "Your e-mail has been verified. You can now sign in."

_TOKEN_IN_MAIL = re.compile(r"token=([A-Za-z0-9_-]+)")


class FakeMailer:
    """Records calls to ``send_email`` and returns a configurable result."""

    def __init__(self) -> None:
        self.result = True
        self.sent: list[tuple[str, EmailContent]] = []
        self._lock = threading.Lock()

    def __call__(self, to: str, message: EmailContent) -> bool:
        with self._lock:
            self.sent.append((to, message))
        return self.result

    def last_token(self) -> str:
        match = _TOKEN_IN_MAIL.search(self.sent[-1][1].body)
        assert match is not None
        return match.group(1)


@pytest.fixture
def mailer(monkeypatch: pytest.MonkeyPatch) -> FakeMailer:
    fake = FakeMailer()
    monkeypatch.setattr(registration, "send_email", fake)
    monkeypatch.setattr(password_reset, "send_email", fake)
    return fake


@pytest.fixture
def client(db: Session, mailer: FakeMailer) -> Iterator[TestClient]:
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


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex}@example.com"


def _make_user(
    db: Session,
    *,
    verified: bool = True,
    password: str | None = GOOD_PASSWORD,
    google_sub: str | None = None,
) -> User:
    settings = get_settings()
    user = User(
        email_normalized=_unique_email(),
        password_hash=hash_password(password) if password is not None else None,
        email_verified_at=datetime.now(UTC) if verified else None,
        google_sub=google_sub,
        terms_version=settings.terms_version,
        privacy_version=settings.privacy_version,
        terms_accepted_at=datetime.now(UTC),
    )
    db.add(user)
    db.flush()
    return user


def _error_code(response: Response) -> str:
    return response.json()["error"]["code"]  # type: ignore[no-any-return]


def _post(
    client: TestClient, path: str, body: dict[str, Any], headers: dict[str, str] | None = None
) -> Response:
    # json.dumps escapes lone surrogates (\\ud800), so hostile strings reach the server as JSON.
    return client.post(
        path,
        content=json.dumps(body),
        headers={"content-type": "application/json", **(headers or {})},
    )


def _xsrf_headers(client: TestClient) -> dict[str, str]:
    value = client.cookies.get(XSRF_COOKIE)
    return {XSRF_HEADER: value} if value else {}


def _login(client: TestClient, email: str, password: str = GOOD_PASSWORD) -> Response:
    body = {"email": email, "password": password}
    return _post(client, "/api/auth/login", body, _xsrf_headers(client))


def _register(client: TestClient, email: str, **overrides: Any) -> Response:
    body: dict[str, Any] = {"email": email, "password": GOOD_PASSWORD, "accept_terms": True}
    body.update(overrides)
    return _post(client, "/api/auth/register", body)


def _session_row(db: Session, raw: str) -> AuthSession:
    row = db.execute(
        select(AuthSession).where(AuthSession.token_hash == hash_secret(raw))
    ).scalar_one()
    return row


# --- AUTH-01 / AUTH-02 / AUTH-14 / AUTH-92: registration -------------------------------------


def test_auth_02_register_new_and_existing_email_return_identical_202(
    client: TestClient, db: Session, mailer: FakeMailer
) -> None:
    existing = _make_user(db)

    new_response = _register(client, _unique_email())
    existing_response = _register(client, existing.email_normalized)

    assert new_response.status_code == 202
    assert existing_response.status_code == 202
    assert new_response.json() == existing_response.json()
    assert new_response.json() == {"message": REGISTER_MESSAGE, "email_delivery": "sent"}
    assert len(mailer.sent) == 1


def test_auth_01_register_creates_unverified_account(client: TestClient, db: Session) -> None:
    email = _unique_email()

    assert _register(client, email).status_code == 202

    user = db.execute(select(User).where(User.email_normalized == email)).scalar_one()
    assert user.email_verified_at is None
    assert _login(client, email).status_code == 403


def test_auth_92_register_with_mail_down_reports_delayed(
    client: TestClient, mailer: FakeMailer
) -> None:
    mailer.result = False

    response = _register(client, _unique_email())

    assert response.status_code == 202
    assert response.json() == {"message": REGISTER_MESSAGE, "email_delivery": "delayed"}


def test_auth_14_register_with_weak_password_returns_password_policy(client: TestClient) -> None:
    weak = "short"
    response = _register(client, _unique_email(), password=weak)

    assert response.status_code == 422
    assert _error_code(response) == "PASSWORD_POLICY"
    assert response.json()["error"]["details"]["violations"]


@pytest.mark.parametrize("accept", [False, None])
def test_auth_01_register_without_terms_returns_terms_not_accepted(
    client: TestClient, accept: bool | None
) -> None:
    body: dict[str, Any] = {"email": _unique_email(), "password": GOOD_PASSWORD}
    if accept is not None:
        body["accept_terms"] = accept

    response = _post(client, "/api/auth/register", body)

    assert response.status_code == 422
    assert _error_code(response) == "TERMS_NOT_ACCEPTED"


def test_auth_01_register_rejects_non_boolean_terms(client: TestClient) -> None:
    response = _register(client, _unique_email(), accept_terms="yes")

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"


# --- AUTH-03 / AUTH-94: verification ---------------------------------------------------------


def test_auth_03_verify_email_marks_account_verified_and_burns_link(
    client: TestClient, db: Session, mailer: FakeMailer
) -> None:
    email = _unique_email()
    _register(client, email)
    token = mailer.last_token()

    response = _post(client, "/api/auth/verify-email", {"token": token})
    again = _post(client, "/api/auth/verify-email", {"token": token})

    assert response.status_code == 200
    assert response.json() == {"message": VERIFIED_MESSAGE}
    assert again.status_code == 400
    assert _error_code(again) == "LINK_INVALID"
    assert _login(client, email).status_code == 200


@pytest.mark.parametrize("token", ["not-a-token", "\x00abc", ""])
def test_auth_94_verify_email_with_invalid_link_returns_link_invalid(
    client: TestClient, token: str
) -> None:
    response = _post(client, "/api/auth/verify-email", {"token": token})

    assert response.status_code == 400
    assert _error_code(response) == "LINK_INVALID"


# --- AUTH-04 / AUTH-92: resend ---------------------------------------------------------------


def test_auth_04_resend_verification_is_neutral(
    client: TestClient, db: Session, mailer: FakeMailer
) -> None:
    unverified = _make_user(db, verified=False)
    verified = _make_user(db)

    responses = [
        _post(client, "/api/auth/resend-verification", {"email": email})
        for email in (unverified.email_normalized, verified.email_normalized, _unique_email())
    ]
    mailer.result = False
    responses.append(
        _post(client, "/api/auth/resend-verification", {"email": unverified.email_normalized})
    )

    assert {r.status_code for r in responses} == {202}
    assert {r.text for r in responses} == {f'{{"message":"{REGISTER_MESSAGE}"}}'}
    assert [to for to, _ in mailer.sent] == [unverified.email_normalized] * 2


# --- AUTH-05 / AUTH-04 / AUTH-90: login ------------------------------------------------------


def test_auth_05_login_sets_session_cookie_and_me_returns_user(
    client: TestClient, db: Session
) -> None:
    user = _make_user(db)

    response = _login(client, user.email_normalized)

    assert response.status_code == 200
    assert f"{SESSION_COOKIE}=" in response.headers["set-cookie"]
    me_body = {
        "id": str(user.id),
        "email": user.email_normalized,
        "has_password": True,
        "google_linked": False,
        "terms_accepted": True,
    }
    assert response.json() == {"user": me_body}
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json() == me_body


def test_auth_05_me_reports_pending_terms(client: TestClient, db: Session) -> None:
    user = _make_user(db, google_sub=f"sub-{uuid.uuid4().hex}")
    user.terms_version = "terms-old"
    db.flush()

    assert _login(client, user.email_normalized).status_code == 200
    me = client.get("/api/auth/me")

    assert me.status_code == 200
    assert me.json()["terms_accepted"] is False
    assert me.json()["google_linked"] is True


@pytest.mark.parametrize("wrong", ["password", "email"])
def test_auth_05_wrong_credentials_return_generic_401(
    client: TestClient, db: Session, wrong: str
) -> None:
    user = _make_user(db)
    email = _unique_email() if wrong == "email" else user.email_normalized
    password = WRONG_PASSWORD if wrong == "password" else GOOD_PASSWORD

    response = _login(client, email, password)

    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "INVALID_CREDENTIALS",
        "message": "Invalid e-mail or password.",
        "details": None,
    }
    assert SESSION_COOKIE not in response.cookies


def test_auth_04_unverified_account_gets_email_not_verified(
    client: TestClient, db: Session
) -> None:
    user = _make_user(db, verified=False)

    response = _login(client, user.email_normalized)

    assert response.status_code == 403
    assert _error_code(response) == "EMAIL_NOT_VERIFIED"
    assert SESSION_COOKIE not in response.cookies


def test_auth_90_repeated_failures_lock_login(client: TestClient, db: Session) -> None:
    user = _make_user(db)
    for _ in range(get_settings().login_max_failures):
        assert _login(client, user.email_normalized, WRONG_PASSWORD).status_code == 401

    response = _login(client, user.email_normalized)

    assert response.status_code == 429
    assert _error_code(response) == "TOO_MANY_ATTEMPTS"


def test_auth_90_login_passes_raw_client_ip_as_client_key(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _make_user(db)
    seen: list[str] = []
    original = auth_local.login_local

    def spy(session: Session, email: str, password: str, client_key: str) -> User:
        seen.append(client_key)
        return original(session, email, password, client_key)

    monkeypatch.setattr(auth_local, "login_local", spy)

    assert _login(client, user.email_normalized).status_code == 200
    assert seen == ["testclient"]


def test_auth_06_login_rotates_the_session_of_the_received_cookie(
    client: TestClient, db: Session
) -> None:
    user = _make_user(db)
    assert _login(client, user.email_normalized).status_code == 200
    first = client.cookies[SESSION_COOKIE]

    second_response = _login(client, user.email_normalized)

    assert second_response.status_code == 200
    second = client.cookies[SESSION_COOKIE]
    assert second != first
    assert _session_row(db, first).revoked_at is not None
    assert _session_row(db, second).revoked_at is None
    client.cookies.set(SESSION_COOKIE, first)
    assert client.get("/api/auth/me").status_code == 401


# --- AUTH-06 / AUTH-17: logout and me --------------------------------------------------------


def test_auth_06_logout_then_me_with_old_cookie_returns_401(
    client: TestClient, db: Session
) -> None:
    user = _make_user(db)
    assert _login(client, user.email_normalized).status_code == 200
    old_session = client.cookies[SESSION_COOKIE]
    old_xsrf = client.cookies[XSRF_COOKIE]

    logout = client.post("/api/auth/logout", headers={XSRF_HEADER: old_xsrf})

    assert logout.status_code == 204
    assert _session_row(db, old_session).revoked_at is not None
    client.cookies.set(SESSION_COOKIE, old_session)
    client.cookies.set(XSRF_COOKIE, old_xsrf)
    me = client.get("/api/auth/me")
    assert me.status_code == 401
    assert _error_code(me) == "AUTH_REQUIRED"


def test_auth_06_logout_without_xsrf_header_is_rejected(client: TestClient, db: Session) -> None:
    user = _make_user(db)
    assert _login(client, user.email_normalized).status_code == 200
    session_cookie = client.cookies[SESSION_COOKIE]

    response = client.post("/api/auth/logout")

    assert response.status_code == 403
    assert _error_code(response) == "CSRF_FAILED"
    assert _session_row(db, session_cookie).revoked_at is None


def test_auth_06_logout_without_cookie_returns_204(client: TestClient) -> None:
    assert client.post("/api/auth/logout").status_code == 204


def test_auth_17_me_without_session_returns_401(client: TestClient) -> None:
    response = client.get("/api/auth/me")

    assert response.status_code == 401
    assert _error_code(response) == "AUTH_REQUIRED"


# --- AUTH-07 / AUTH-08 / AUTH-94: password reset ---------------------------------------------


def test_auth_07_forgot_password_is_neutral(
    client: TestClient, db: Session, mailer: FakeMailer
) -> None:
    user = _make_user(db)

    existing = _post(client, "/api/auth/forgot-password", {"email": user.email_normalized})
    unknown = _post(client, "/api/auth/forgot-password", {"email": _unique_email()})
    malformed = _post(client, "/api/auth/forgot-password", {"email": "\x00a@example.com"})

    assert existing.status_code == unknown.status_code == malformed.status_code == 202
    assert existing.text == unknown.text == malformed.text
    assert existing.json() == {"message": FORGOT_MESSAGE}
    assert [to for to, _ in mailer.sent] == [user.email_normalized]


def test_auth_08_reset_password_changes_password_and_burns_link(
    client: TestClient, db: Session, mailer: FakeMailer
) -> None:
    user = _make_user(db)
    _post(client, "/api/auth/forgot-password", {"email": user.email_normalized})
    token = mailer.last_token()

    response = _post(
        client, "/api/auth/reset-password", {"token": token, "new_password": NEW_PASSWORD}
    )
    again = _post(
        client, "/api/auth/reset-password", {"token": token, "new_password": NEW_PASSWORD}
    )

    assert response.status_code == 204
    assert again.status_code == 400
    assert _error_code(again) == "LINK_INVALID"
    assert _login(client, user.email_normalized, GOOD_PASSWORD).status_code == 401
    assert _login(client, user.email_normalized, NEW_PASSWORD).status_code == 200


def test_auth_14_reset_with_weak_password_keeps_link_usable(
    client: TestClient, db: Session, mailer: FakeMailer
) -> None:
    user = _make_user(db)
    _post(client, "/api/auth/forgot-password", {"email": user.email_normalized})
    token = mailer.last_token()

    weak = _post(client, "/api/auth/reset-password", {"token": token, "new_password": "short"})
    strong = _post(
        client, "/api/auth/reset-password", {"token": token, "new_password": NEW_PASSWORD}
    )

    assert weak.status_code == 422
    assert _error_code(weak) == "PASSWORD_POLICY"
    assert strong.status_code == 204


@pytest.mark.parametrize("token", ["unknown-token", "\x00"])
def test_auth_94_reset_with_invalid_link_returns_link_invalid(
    client: TestClient, token: str
) -> None:
    response = _post(
        client, "/api/auth/reset-password", {"token": token, "new_password": NEW_PASSWORD}
    )

    assert response.status_code == 400
    assert _error_code(response) == "LINK_INVALID"


def test_auth_08_reset_with_unencodable_password_returns_validation_error(
    client: TestClient,
) -> None:
    response = _post(
        client, "/api/auth/reset-password", {"token": "abc", "new_password": "pass\ud800word!"}
    )

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"


# --- Hostile input never becomes a 500 -------------------------------------------------------


@pytest.mark.parametrize("value", ["\x00", "\ud800", "a\x00@example.com", "\ud800@example.com"])
def test_auth_05_hostile_strings_never_cause_server_errors(client: TestClient, value: str) -> None:
    responses = [
        _post(client, "/api/auth/register", {"email": value, "password": GOOD_PASSWORD}),
        _post(
            client,
            "/api/auth/register",
            {"email": _unique_email(), "password": value * 8, "accept_terms": True},
        ),
        _post(client, "/api/auth/login", {"email": value, "password": value}),
        _post(client, "/api/auth/resend-verification", {"email": value}),
        _post(client, "/api/auth/forgot-password", {"email": value}),
    ]

    assert all(r.status_code < 500 for r in responses), [r.status_code for r in responses]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/auth/register", {"email": "\ud800@example.com", "password": GOOD_PASSWORD}),
        ("/api/auth/login", {"email": "a@example.com", "password": "\ud800"}),
        ("/api/auth/resend-verification", {"email": "\ud800"}),
        ("/api/auth/forgot-password", {"email": "\ud800"}),
        ("/api/auth/verify-email", {"token": "\ud800"}),
        ("/api/auth/reset-password", {"token": "\ud800", "new_password": NEW_PASSWORD}),
    ],
)
def test_auth_05_lone_surrogates_are_rejected_as_validation_error(
    client: TestClient, path: str, body: dict[str, str]
) -> None:
    # Rejected at the boundary for every account alike, so nothing is revealed.
    response = _post(client, path, body)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert "\ud800" not in response.text


@pytest.mark.parametrize("body", ["{", "[]", '"text"', '{"email": 5, "password": true}'])
def test_auth_05_malformed_json_returns_validation_error(client: TestClient, body: str) -> None:
    response = client.post(
        "/api/auth/login", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"


# --- LAC-32: field and body size limits ------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/auth/register", {"email": "a" * 1025, "password": GOOD_PASSWORD}),
        ("/api/auth/register", {"email": _unique_email(), "password": "p" * 1025}),
        ("/api/auth/login", {"email": _unique_email(), "password": "p" * 1025}),
        ("/api/auth/resend-verification", {"email": "a" * 1025}),
        ("/api/auth/forgot-password", {"email": "a" * 1025}),
        ("/api/auth/verify-email", {"token": "t" * 1025}),
        ("/api/auth/reset-password", {"token": "t" * 1025, "new_password": NEW_PASSWORD}),
        ("/api/auth/reset-password", {"token": "t", "new_password": "p" * 1025}),
    ],
)
def test_auth_14_fields_over_1024_chars_return_validation_error(
    client: TestClient, path: str, body: dict[str, str]
) -> None:
    response = _post(client, path, body)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"


def test_auth_14_password_of_1024_chars_is_accepted(client: TestClient) -> None:
    response = _register(client, _unique_email(), password="Zq9!" * 256)

    assert response.status_code == 202


def test_auth_05_body_over_16_kib_returns_validation_error(client: TestClient, db: Session) -> None:
    user = _make_user(db)
    padding = "x" * (16 * 1024)

    response = _post(
        client,
        "/api/auth/login",
        {"email": user.email_normalized, "password": GOOD_PASSWORD, "padding": padding},
    )

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert SESSION_COOKIE not in response.cookies


def test_auth_05_chunked_body_over_16_kib_returns_validation_error(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"email": "a@example.com", "password": "'
        for _ in range(20):
            yield b"p" * 1024
        yield b'"}'

    response = client.post(
        "/api/auth/forgot-password",
        content=chunks(),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"


@pytest.mark.parametrize("declared", ["abc", "-1", "99999999999"])
def test_auth_05_bad_content_length_is_rejected(client: TestClient, declared: str) -> None:
    response = client.post(
        "/api/auth/forgot-password",
        content=b'{"email": "a@example.com"}',
        headers={"content-type": "application/json", "content-length": declared},
    )

    assert response.status_code in {400, 422}


# --- AUTH-95: no token in logs ---------------------------------------------------------------

_UNMASKED_TOKEN = re.compile(r"token=(?!\[REDACTED\])[^\s&\"']+")


def test_auth_95_no_log_record_contains_an_unmasked_token(
    client: TestClient, db: Session, mailer: FakeMailer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    email = _unique_email()

    _register(client, email)
    verify_token = mailer.last_token()
    _post(client, "/api/auth/resend-verification", {"email": email})
    _post(client, "/api/auth/verify-email", {"token": verify_token})
    _login(client, email)
    client.get("/api/auth/me")
    client.post("/api/auth/logout", headers=_xsrf_headers(client))
    _post(client, "/api/auth/forgot-password", {"email": email})
    reset_token = mailer.last_token()
    _post(client, "/api/auth/reset-password", {"token": reset_token, "new_password": "short"})
    _post(client, "/api/auth/reset-password", {"token": reset_token, "new_password": NEW_PASSWORD})

    assert caplog.records
    for record in caplog.records:
        text = f"{record.getMessage()} {record.__dict__}"
        assert _UNMASKED_TOKEN.search(text) is None, record.name
        assert verify_token not in text
        assert reset_token not in text
        assert GOOD_PASSWORD not in text
        assert NEW_PASSWORD not in text
