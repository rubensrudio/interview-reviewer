"""Integration tests for authenticated sessions and the current-user dependency.

Covers CT-11 and AUTH-06 (logout invalidates the session), AUTH-17 (anonymous access is
denied), AUTH-16 (only the session owner is resolved) and the CSRF double-submit check (DA-5).
"""

import hashlib
import hmac
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, Request, Response
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, CurrentUserPendingTerms, DbSession
from app.auth.sessions import (
    SESSION_COOKIE,
    XSRF_COOKIE,
    XSRF_HEADER,
    create_auth_session,
    get_current_user,
    revoke_all_sessions,
    revoke_auth_session,
)
from app.auth.tokens import hash_secret
from app.config import get_settings
from app.db import get_db
from app.errors import AppError, register_error_handlers
from app.models.account import AuthSession, User


def _user(db: Session, *, accepted: bool = True) -> User:
    settings = get_settings()
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    if accepted:
        user.terms_version = settings.terms_version
        user.privacy_version = settings.privacy_version
        user.terms_accepted_at = datetime.now(UTC)
    db.add(user)
    db.flush()
    return user


def _build_app(db: Session) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    def override_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = override_db

    @app.post("/test/login/{user_id}")
    def login(user_id: uuid.UUID, response: Response, session: DbSession) -> dict[str, str]:
        user = session.get(User, user_id)
        assert user is not None
        create_auth_session(session, user, response)
        session.commit()
        return {"status": "ok"}

    @app.get("/test/me")
    def me(user: CurrentUser) -> dict[str, str]:
        return {"id": str(user.id)}

    @app.post("/test/me")
    def me_post(user: CurrentUser) -> dict[str, str]:
        return {"id": str(user.id)}

    @app.get("/test/pending")
    def pending(user: CurrentUserPendingTerms) -> dict[str, str]:
        return {"id": str(user.id)}

    @app.post("/test/pending")
    def pending_post(user: CurrentUserPendingTerms) -> dict[str, str]:
        return {"id": str(user.id)}

    @app.api_route("/test/me", methods=["HEAD", "OPTIONS"])
    def me_head_options(user: CurrentUser) -> dict[str, str]:
        return {"id": str(user.id)}

    @app.post("/test/logout")
    def logout(
        request: Request, response: Response, session: Annotated[Session, Depends(get_db)]
    ) -> dict[str, str]:
        revoke_auth_session(session, request, response)
        session.commit()
        return {"status": "ok"}

    return app


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    with TestClient(_build_app(db)) as test_client:
        yield test_client


def _login(client: TestClient, user: User) -> tuple[str, str]:
    response = client.post(f"/test/login/{user.id}")
    assert response.status_code == 200
    return response.cookies[SESSION_COOKIE], response.cookies[XSRF_COOKIE]


def _error_code(response: object) -> str:
    return response.json()["error"]["code"]  # type: ignore[attr-defined,no-any-return]


def test_create_auth_session_sets_hardened_cookies_and_stores_only_hash(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    response = client.post(f"/test/login/{user.id}")

    set_cookies = response.headers.get_list("set-cookie")
    session_header = next(h for h in set_cookies if h.startswith(f"{SESSION_COOKIE}="))
    xsrf_header = next(h for h in set_cookies if h.startswith(f"{XSRF_COOKIE}="))
    assert "HttpOnly" in session_header
    assert "samesite=lax" in session_header.lower()
    assert "Path=/" in session_header
    assert "HttpOnly" not in xsrf_header  # Angular must be able to read it.

    raw = response.cookies[SESSION_COOKIE]
    row = db.execute(select(AuthSession).where(AuthSession.user_id == user.id)).scalar_one()
    assert row.token_hash == hash_secret(raw)
    assert row.token_hash != raw
    ttl = timedelta(hours=get_settings().session_ttl_hours)
    assert datetime.now(UTC) + ttl - timedelta(minutes=1) < row.expires_at
    assert row.expires_at <= datetime.now(UTC) + ttl


def test_secure_flag_follows_cookie_secure(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "cookie_secure", True)
    user = _user(db)
    response = client.post(f"/test/login/{user.id}")
    for header in response.headers.get_list("set-cookie"):
        assert "Secure" in header


def test_auth_17_request_without_cookie_gets_401(client: TestClient) -> None:
    response = client.get("/test/me")
    assert response.status_code == 401
    assert _error_code(response) == "AUTH_REQUIRED"

    response = client.get("/test/pending")
    assert response.status_code == 401
    assert _error_code(response) == "AUTH_REQUIRED"


def test_auth_16_authenticated_request_resolves_session_owner(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    _login(client, user)
    response = client.get("/test/me")
    assert response.status_code == 200
    assert response.json() == {"id": str(user.id)}


def test_auth_06_after_revoke_old_cookie_gets_401(client: TestClient, db: Session) -> None:
    user = _user(db)
    session_raw, xsrf = _login(client, user)

    response = client.post("/test/logout", headers={XSRF_HEADER: xsrf})
    assert response.status_code == 200
    cleared = response.headers.get_list("set-cookie")
    assert any(h.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" in h for h in cleared)

    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, session_raw)
    response = client.get("/test/me")
    assert response.status_code == 401
    assert _error_code(response) == "AUTH_REQUIRED"
    db.expire_all()
    row = db.execute(select(AuthSession).where(AuthSession.user_id == user.id)).scalar_one()
    assert row.revoked_at is not None


def test_revoke_auth_session_without_cookie_only_clears_cookies(client: TestClient) -> None:
    response = client.post("/test/logout")
    assert response.status_code == 200


def test_revoke_all_sessions_invalidates_every_session_of_the_user(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    other = _user(db)
    first, _ = _login(client, user)
    second, _ = _login(client, user)
    other_raw, _ = _login(client, other)

    revoke_all_sessions(db, user.id)
    db.commit()

    for raw in (first, second):
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, raw)
        assert client.get("/test/me").status_code == 401
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, other_raw)
    assert client.get("/test/me").status_code == 200


def test_expired_session_gets_401(client: TestClient, db: Session) -> None:
    user = _user(db)
    _login(client, user)
    row = db.execute(select(AuthSession).where(AuthSession.user_id == user.id)).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()

    response = client.get("/test/me")
    assert response.status_code == 401
    assert _error_code(response) == "AUTH_REQUIRED"


def test_user_with_deletion_requested_gets_401(client: TestClient, db: Session) -> None:
    user = _user(db)
    _login(client, user)
    user.deletion_requested_at = datetime.now(UTC)
    db.flush()

    assert client.get("/test/me").status_code == 401
    assert client.get("/test/pending").status_code == 401


def test_user_without_terms_gets_403_and_passes_pending_terms(
    client: TestClient, db: Session
) -> None:
    user = _user(db, accepted=False)
    _, xsrf = _login(client, user)

    response = client.get("/test/me")
    assert response.status_code == 403
    assert _error_code(response) == "TERMS_REQUIRED"

    response = client.get("/test/pending")
    assert response.status_code == 200
    assert response.json() == {"id": str(user.id)}
    response = client.post("/test/pending", headers={XSRF_HEADER: xsrf})
    assert response.status_code == 200


def test_user_with_outdated_terms_version_gets_403(client: TestClient, db: Session) -> None:
    user = _user(db)
    user.terms_version = "terms-old"
    db.flush()
    _login(client, user)

    response = client.get("/test/me")
    assert response.status_code == 403
    assert _error_code(response) == "TERMS_REQUIRED"


def test_csrf_post_without_header_gets_403(client: TestClient, db: Session) -> None:
    user = _user(db)
    _login(client, user)

    response = client.post("/test/me")
    assert response.status_code == 403
    assert _error_code(response) == "CSRF_FAILED"
    response = client.post("/test/pending")
    assert response.status_code == 403
    assert _error_code(response) == "CSRF_FAILED"


def test_csrf_post_with_header_different_from_cookie_gets_403(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    _login(client, user)

    response = client.post("/test/me", headers={XSRF_HEADER: "a" * 64})
    assert response.status_code == 403
    assert _error_code(response) == "CSRF_FAILED"


def test_csrf_token_is_bound_to_the_session(client: TestClient, db: Session) -> None:
    user = _user(db)
    _login(client, user)
    # An attacker able to plant a cookie still cannot forge a pair matching the session.
    client.cookies.set(XSRF_COOKIE, "b" * 64)
    response = client.post("/test/me", headers={XSRF_HEADER: "b" * 64})
    assert response.status_code == 403
    assert _error_code(response) == "CSRF_FAILED"


def test_csrf_post_with_matching_header_passes(client: TestClient, db: Session) -> None:
    user = _user(db)
    _, xsrf = _login(client, user)

    response = client.post("/test/me", headers={XSRF_HEADER: xsrf})
    assert response.status_code == 200


def test_unauthenticated_post_gets_401_before_csrf(client: TestClient) -> None:
    response = client.post("/test/me")
    assert response.status_code == 401
    assert _error_code(response) == "AUTH_REQUIRED"


@pytest.mark.parametrize(
    "cookie",
    ["", "not a token!", "x" * 5000, "ééé", "does-not-exist"],
)
def test_malformed_or_unknown_session_cookie_gets_401(client: TestClient, cookie: str) -> None:
    raw_header = f"{SESSION_COOKIE}={cookie}".encode("latin-1")
    response = client.get("/test/me", headers={"Cookie": raw_header})
    assert response.status_code == 401
    assert _error_code(response) == "AUTH_REQUIRED"


@pytest.mark.parametrize("header", ["éé", "x" * 5000, "bad value"])
def test_malformed_xsrf_header_gets_403(client: TestClient, db: Session, header: str) -> None:
    user = _user(db)
    _login(client, user)
    response = client.post("/test/me", headers={XSRF_HEADER: header.encode("latin-1")})
    assert response.status_code == 403
    assert _error_code(response) == "CSRF_FAILED"


def test_lone_surrogate_cookie_is_rejected_without_error(db: Session) -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
    }
    request = Request(scope)
    request._cookies = {SESSION_COOKIE: "\ud800"}

    with pytest.raises(AppError) as exc_info:
        get_current_user(request, db)
    assert exc_info.value.code == "AUTH_REQUIRED"


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_csrf_head_and_options_without_header_get_403(
    client: TestClient, db: Session, method: str
) -> None:
    user = _user(db)
    _, xsrf = _login(client, user)

    response = client.request(method, "/test/me")
    assert response.status_code == 403
    response = client.request(method, "/test/me", headers={XSRF_HEADER: xsrf})
    assert response.status_code == 200


def test_auth_06_logout_without_xsrf_header_gets_403_and_keeps_session(
    client: TestClient, db: Session
) -> None:
    user = _user(db)
    _login(client, user)

    response = client.post("/test/logout")
    assert response.status_code == 403
    assert _error_code(response) == "CSRF_FAILED"
    db.expire_all()
    row = db.execute(select(AuthSession).where(AuthSession.user_id == user.id)).scalar_one()
    assert row.revoked_at is None
    assert client.get("/test/me").status_code == 200


def _derived_xsrf(raw_session: str) -> str:
    return hmac.new(raw_session.encode("ascii"), b"ir-xsrf-v1", hashlib.sha256).hexdigest()


@pytest.mark.parametrize("revoked_first", [False, True])
def test_auth_06_logout_with_inactive_session_and_valid_xsrf_clears_cookies(
    client: TestClient, db: Session, revoked_first: bool
) -> None:
    if revoked_first:
        raw, xsrf = _login(client, _user(db))
        assert client.post("/test/logout", headers={XSRF_HEADER: xsrf}).status_code == 200
    else:
        raw = "does-not-exist"
        xsrf = _derived_xsrf(raw)
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, raw)
    client.cookies.set(XSRF_COOKIE, xsrf)

    response = client.post("/test/logout", headers={XSRF_HEADER: xsrf})
    assert response.status_code == 200
    cleared = response.headers.get_list("set-cookie")
    assert any(h.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" in h for h in cleared)


def test_auth_06_logout_with_inactive_session_without_xsrf_header_gets_403(
    client: TestClient,
) -> None:
    raw = "does-not-exist"
    client.cookies.set(SESSION_COOKIE, raw)
    client.cookies.set(XSRF_COOKIE, _derived_xsrf(raw))

    response = client.post("/test/logout")
    assert response.status_code == 403
    assert _error_code(response) == "CSRF_FAILED"


def test_auth_06_logout_with_malformed_session_cookie_only_clears_cookies(
    client: TestClient,
) -> None:
    raw_header = f"{SESSION_COOKIE}=not a token!".encode("latin-1")
    response = client.post("/test/logout", headers={"Cookie": raw_header})
    assert response.status_code == 200
    cleared = response.headers.get_list("set-cookie")
    assert any(h.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" in h for h in cleared)
