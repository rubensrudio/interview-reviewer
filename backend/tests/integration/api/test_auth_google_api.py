"""Integration tests for the Google sign-in and terms acceptance routes (TASK-022, plan 8.1).

Covers AUTH-10, AUTH-11, AUTH-12, AUTH-13, AUTH-15 and AUTH-93 at the HTTP layer, plus the
request size limits of LAC-32, LAC-35 (linked account without current terms) and LAC-36 (the
link token survives a wrong password). The Google exchange is replaced by a fake identity.
"""

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import auth_google
from app.auth.google_oidc import STATE_COOKIE_NAME, GoogleIdentity
from app.auth.passwords import hash_password
from app.auth.sessions import SESSION_COOKIE, XSRF_COOKIE, XSRF_HEADER, get_current_user
from app.auth.tokens import hash_secret
from app.config import get_settings
from app.db import get_db
from app.errors import GOOGLE_AUTH_FAILED, AppError
from app.main import create_app
from app.models.account import AuthSession, OneTimeToken, User

GOOD_PASSWORD = "correct horse battery staple 42"  # noqa: S105 - test fixture value
WRONG_PASSWORD = "wrong horse battery staple 44"  # noqa: S105 - test fixture value
FRONTEND = "http://localhost:4200"
LOGIN_FAILED = f"{FRONTEND}/login?error=google_failed"


class FakeGoogle:
    """Stands in for ``exchange_callback``: returns ``identity`` or raises the Google error."""

    def __init__(self) -> None:
        self.identity: GoogleIdentity | None = None

    def __call__(self, _request: Any) -> GoogleIdentity:
        if self.identity is None:
            raise AppError.from_catalog(GOOGLE_AUTH_FAILED)
        return self.identity


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> FakeGoogle:
    fake = FakeGoogle()
    monkeypatch.setattr(auth_google, "exchange_callback", fake)
    return fake


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    app = create_app()
    probe = APIRouter()

    @probe.get("/api/test/private")
    def private(user: User = Depends(get_current_user)) -> dict[str, str]:  # noqa: B008
        return {"id": str(user.id)}

    app.include_router(probe)

    def override_db() -> Iterator[Session]:
        # Same contract as app.db.get_db: roll back what the request left uncommitted.
        try:
            yield db
        except Exception:
            db.rollback()
            raise

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False, follow_redirects=False) as test_client:
        yield test_client


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex}@example.com"


def _unique_sub() -> str:
    return str(uuid.uuid4().int)[:21]


def _identity(email: str, sub: str | None = None) -> GoogleIdentity:
    return GoogleIdentity(sub=sub or _unique_sub(), email=email, email_verified=True)


def _make_user(
    db: Session, *, google_sub: str | None = None, password: str | None = GOOD_PASSWORD
) -> User:
    settings = get_settings()
    user = User(
        email_normalized=_unique_email(),
        password_hash=hash_password(password) if password is not None else None,
        email_verified_at=datetime.now(UTC),
        google_sub=google_sub,
        terms_version=settings.terms_version,
        privacy_version=settings.privacy_version,
        terms_accepted_at=datetime.now(UTC),
    )
    db.add(user)
    db.flush()
    return user


def _user_count(db: Session) -> int:
    return db.execute(select(func.count()).select_from(User)).scalar_one()


def _error_code(response: Response) -> str:
    return response.json()["error"]["code"]  # type: ignore[no-any-return]


def _post(
    client: TestClient, path: str, body: Any, headers: dict[str, str] | None = None
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


def _set_cookie_headers(response: Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _sets_session_cookie(response: Response) -> bool:
    return any(
        header.startswith(f"{SESSION_COOKIE}=") and 'ir_session=""' not in header
        for header in _set_cookie_headers(response)
    )


def _clears_state_cookie(response: Response) -> bool:
    return any(
        header.startswith(f"{STATE_COOKIE_NAME}=") and "Max-Age=0" in header
        for header in _set_cookie_headers(response)
    )


def _callback(client: TestClient) -> Response:
    return client.get("/api/auth/google/callback?code=abc&state=xyz")


def _link_token_from(response: Response) -> str:
    location = response.headers["location"]
    parts = urlsplit(location)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{FRONTEND}/link-google"
    return parse_qs(parts.query)["token"][0]


def _link(client: TestClient, token: str, password: str) -> Response:
    return _post(
        client,
        "/api/auth/google/link",
        {"token": token, "password": password},
        _xsrf_headers(client),
    )


# --- GET /api/auth/google/start ---------------------------------------------------------------


def test_auth_10_start_redirects_to_google_with_state_cookie(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IR_GOOGLE_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("IR_GOOGLE_CLIENT_SECRET", "client-secret")
    get_settings.cache_clear()
    try:
        response = client.get("/api/auth/google/start")
    finally:
        monkeypatch.delenv("IR_GOOGLE_CLIENT_ID")
        monkeypatch.delenv("IR_GOOGLE_CLIENT_SECRET")
        get_settings.cache_clear()

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://accounts.google.com/")
    assert any(h.startswith(f"{STATE_COOKIE_NAME}=") for h in _set_cookie_headers(response))
    assert response.headers["cache-control"] == "no-store"


def test_auth_93_start_without_google_config_redirects_to_login(client: TestClient) -> None:
    response = client.get("/api/auth/google/start")

    assert response.status_code == 302
    assert response.headers["location"] == LOGIN_FAILED


# --- GET /api/auth/google/callback ------------------------------------------------------------


def test_auth_10_new_identity_opens_session_and_requires_terms(
    client: TestClient, db: Session, google: FakeGoogle
) -> None:
    email = _unique_email()
    google.identity = _identity(email)

    response = _callback(client)

    assert response.status_code == 302
    assert response.headers["location"] == f"{FRONTEND}/accept-terms"
    assert _sets_session_cookie(response)
    assert _clears_state_cookie(response)
    user = db.execute(select(User).where(User.email_normalized == email)).scalar_one()
    assert user.google_sub == google.identity.sub
    assert user.email_verified_at is not None
    assert user.terms_accepted_at is None

    blocked = client.get("/api/test/private")
    assert blocked.status_code == 403
    assert _error_code(blocked) == "TERMS_REQUIRED"

    accepted = _post(client, "/api/account/terms", {"accept": True}, _xsrf_headers(client))
    assert accepted.status_code == 204

    assert client.get("/api/test/private").status_code == 200


def test_auth_15_terms_acceptance_stores_date_and_versions(
    client: TestClient, db: Session, google: FakeGoogle
) -> None:
    email = _unique_email()
    google.identity = _identity(email)
    _callback(client)
    before = datetime.now(UTC)

    response = _post(client, "/api/account/terms", {"accept": True}, _xsrf_headers(client))

    assert response.status_code == 204
    user = db.execute(select(User).where(User.email_normalized == email)).scalar_one()
    db.refresh(user)
    settings = get_settings()
    assert user.terms_version == settings.terms_version
    assert user.privacy_version == settings.privacy_version
    assert user.terms_accepted_at is not None and user.terms_accepted_at >= before


@pytest.mark.parametrize("body", [{"accept": False}, {}, {"accept": "true"}, {"accept": 1}])
def test_auth_15_terms_without_explicit_true_is_rejected(
    client: TestClient, db: Session, google: FakeGoogle, body: dict[str, Any]
) -> None:
    email = _unique_email()
    google.identity = _identity(email)
    _callback(client)

    response = _post(client, "/api/account/terms", body, _xsrf_headers(client))

    assert response.status_code == 422
    user = db.execute(select(User).where(User.email_normalized == email)).scalar_one()
    db.refresh(user)
    assert user.terms_accepted_at is None


def test_auth_15_terms_requires_session_and_csrf(client: TestClient, google: FakeGoogle) -> None:
    anonymous = _post(client, "/api/account/terms", {"accept": True})
    assert anonymous.status_code == 401
    assert _error_code(anonymous) == "AUTH_REQUIRED"

    google.identity = _identity(_unique_email())
    _callback(client)
    no_csrf = _post(client, "/api/account/terms", {"accept": True})
    assert no_csrf.status_code == 403
    assert _error_code(no_csrf) == "CSRF_FAILED"


def test_auth_13_linked_identity_signs_in_to_same_account(
    client: TestClient, db: Session, google: FakeGoogle
) -> None:
    sub = _unique_sub()
    user = _make_user(db, google_sub=sub)
    google.identity = _identity(user.email_normalized, sub)

    response = _callback(client)

    assert response.status_code == 302
    assert response.headers["location"] == f"{FRONTEND}/resumes"
    assert _sets_session_cookie(response)
    assert client.get("/api/test/private").json() == {"id": str(user.id)}


def test_auth_13_linked_identity_without_current_terms_goes_to_accept_terms(
    client: TestClient, db: Session, google: FakeGoogle
) -> None:
    sub = _unique_sub()
    user = _make_user(db, google_sub=sub)
    user.terms_version = "terms-old"
    db.flush()
    google.identity = _identity(user.email_normalized, sub)

    response = _callback(client)

    assert response.headers["location"] == f"{FRONTEND}/accept-terms"
    assert _sets_session_cookie(response)


def test_auth_13_callback_revokes_the_session_of_the_received_cookie(
    client: TestClient, db: Session, google: FakeGoogle
) -> None:
    sub = _unique_sub()
    user = _make_user(db, google_sub=sub)
    google.identity = _identity(user.email_normalized, sub)
    _callback(client)
    first = client.cookies[SESSION_COOKIE]

    _callback(client)

    second = client.cookies[SESSION_COOKIE]
    assert second != first
    old = db.execute(
        select(AuthSession).where(AuthSession.token_hash == hash_secret(first))
    ).scalar_one()
    db.refresh(old)
    assert old.revoked_at is not None


def test_auth_11_local_email_redirects_to_link_without_session(
    client: TestClient, db: Session, google: FakeGoogle
) -> None:
    user = _make_user(db)
    google.identity = _identity(user.email_normalized)

    response = _callback(client)

    assert response.status_code == 302
    token = _link_token_from(response)
    assert token
    assert not _sets_session_cookie(response)
    assert SESSION_COOKIE not in client.cookies
    assert _clears_state_cookie(response)
    assert response.headers["referrer-policy"] == "no-referrer"
    db.refresh(user)
    assert user.google_sub is None
    stored = db.execute(
        select(OneTimeToken).where(OneTimeToken.token_hash == hash_secret(token))
    ).scalar_one()
    assert stored.user_id == user.id


def test_auth_93_callback_failure_redirects_to_login_without_account(
    client: TestClient, db: Session, google: FakeGoogle
) -> None:
    before = _user_count(db)
    google.identity = None

    response = _callback(client)

    assert response.status_code == 302
    assert response.headers["location"] == LOGIN_FAILED
    assert not _sets_session_cookie(response)
    assert _clears_state_cookie(response)
    assert _user_count(db) == before


def test_auth_93_cancelled_at_google_redirects_to_login(client: TestClient, db: Session) -> None:
    # Real exchange_callback: Google sends ?error=access_denied when the user cancels.
    before = _user_count(db)

    response = client.get("/api/auth/google/callback?error=access_denied&state=x")

    assert response.status_code == 302
    assert response.headers["location"] == LOGIN_FAILED
    assert _user_count(db) == before


def test_auth_93_rejected_identity_redirects_to_login(
    client: TestClient, db: Session, google: FakeGoogle
) -> None:
    other = _make_user(db, google_sub=_unique_sub())
    db.commit()  # the route rolls back on failure; keep the fixture account
    google.identity = _identity(other.email_normalized)  # e-mail linked to another sub
    before = _user_count(db)

    response = _callback(client)

    assert response.headers["location"] == LOGIN_FAILED
    assert not _sets_session_cookie(response)
    assert _user_count(db) == before


# --- POST /api/auth/google/link ---------------------------------------------------------------


def _pending_link(client: TestClient, db: Session, google: FakeGoogle) -> tuple[User, str, str]:
    user = _make_user(db)
    sub = _unique_sub()
    google.identity = _identity(user.email_normalized, sub)
    return user, sub, _link_token_from(_callback(client))


def test_auth_12_link_with_wrong_password_returns_401_without_session(
    client: TestClient, db: Session, google: FakeGoogle
) -> None:
    user, _sub, token = _pending_link(client, db, google)

    response = _link(client, token, WRONG_PASSWORD)

    assert response.status_code == 401
    assert _error_code(response) == "INVALID_CREDENTIALS"
    assert not _sets_session_cookie(response)
    assert SESSION_COOKIE not in client.cookies
    db.refresh(user)
    assert user.google_sub is None


def test_auth_11_link_with_right_password_links_and_opens_session(
    client: TestClient, db: Session, google: FakeGoogle
) -> None:
    user, sub, token = _pending_link(client, db, google)

    # LAC-36: the token is still usable after a wrong password.
    assert _link(client, token, WRONG_PASSWORD).status_code == 401
    response = _link(client, token, GOOD_PASSWORD)

    assert response.status_code == 200
    body = response.json()["user"]
    assert body["id"] == str(user.id)
    assert body["google_linked"] is True
    assert body["terms_accepted"] is True
    assert _sets_session_cookie(response)
    assert response.cookies[XSRF_COOKIE]
    db.refresh(user)
    assert user.google_sub == sub
    assert client.get("/api/test/private").status_code == 200
    assert _link(client, token, GOOD_PASSWORD).status_code == 400


def test_auth_11_link_with_unknown_token_returns_link_invalid(client: TestClient) -> None:
    response = _link(client, "a" * 43, GOOD_PASSWORD)

    assert response.status_code == 400
    assert _error_code(response) == "LINK_INVALID"


def test_auth_12_link_with_hostile_input_never_returns_500(
    client: TestClient, db: Session, google: FakeGoogle
) -> None:
    _user, _sub, token = _pending_link(client, db, google)

    assert _link(client, token, "\ud800\x00").status_code in {401, 422}
    assert _link(client, "\ud800\x00", GOOD_PASSWORD).status_code in {400, 422}
    assert _link(client, token, "pass\x00word").status_code == 401
    assert _post(client, "/api/auth/google/link", {"token": 1, "password": []}).status_code == 422
    assert _post(client, "/api/auth/google/link", ["x"]).status_code == 422


def test_auth_12_link_limits_field_and_body_size(
    client: TestClient, db: Session, google: FakeGoogle
) -> None:
    _user, _sub, token = _pending_link(client, db, google)

    long_field = _link(client, token, "x" * 1025)
    big_body = _post(
        client, "/api/auth/google/link", {"token": token, "password": "p", "pad": "x" * 17000}
    )

    assert long_field.status_code == 422
    assert big_body.status_code == 422
    assert _error_code(big_body) == "VALIDATION_ERROR"


def test_auth_12_link_failures_are_throttled_per_origin(
    client: TestClient, db: Session, google: FakeGoogle
) -> None:
    limit = get_settings().login_max_failures
    for _ in range(limit):
        _user, _sub, token = _pending_link(client, db, google)
        assert _link(client, token, WRONG_PASSWORD).status_code == 401

    user, _sub, token = _pending_link(client, db, google)
    response = _link(client, token, GOOD_PASSWORD)

    assert response.status_code == 429
    assert _error_code(response) == "TOO_MANY_ATTEMPTS"
    db.refresh(user)
    assert user.google_sub is None
