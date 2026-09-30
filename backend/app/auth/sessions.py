"""Authenticated sessions: opaque cookie, hashed server-side record and CSRF (CT-11, DA-5).

The raw session secret only ever travels in the HttpOnly ``ir_session`` cookie; the database
keeps its SHA-256 digest, so logout (or ``revoke_all_sessions``) invalidates it on the server.
CSRF uses the double-submit pattern supported natively by Angular: a readable ``XSRF-TOKEN``
cookie that must be echoed in the ``X-XSRF-TOKEN`` header on every non-safe authenticated
request. The XSRF value is derived from the session secret, so a cookie planted by an attacker
cannot produce a valid pair. Callers own the transaction (CT-2): nothing here commits.
"""

import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth.tokens import hash_secret, new_secret
from app.config import get_settings
from app.db import get_db
from app.errors import AUTH_REQUIRED, CSRF_FAILED, TERMS_REQUIRED, AppError
from app.models.account import AuthSession, User
from app.observability import log_event

SESSION_COOKIE = "ir_session"
XSRF_COOKIE = "XSRF-TOKEN"  # noqa: S105 - cookie name, not a secret
XSRF_HEADER = "X-XSRF-TOKEN"  # noqa: S105 - header name, not a secret
COOKIE_PATH = "/"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# new_secret() yields 43 base64url characters; the XSRF value is a 64-char hex digest.
# Values are validated before any encode/hash/compare so malformed input (non-ASCII, lone
# surrogates, huge strings) is rejected instead of raising.
_SESSION_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,256}")
_XSRF_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")
_XSRF_CONTEXT = b"ir-xsrf-v1"


def _auth_required(reason: str) -> AppError:
    log_event("auth.session_rejected", reason=reason)
    return AppError.from_catalog(AUTH_REQUIRED)


def _csrf_failed(reason: str) -> AppError:
    log_event("auth.csrf_rejected", reason=reason)
    return AppError.from_catalog(CSRF_FAILED)


def _xsrf_for(raw_session: str) -> str:
    """Derive the XSRF value bound to a (validated, ASCII) session secret."""
    return hmac.new(raw_session.encode("ascii"), _XSRF_CONTEXT, hashlib.sha256).hexdigest()


def _valid_session_token(value: object) -> str | None:
    if isinstance(value, str) and _SESSION_TOKEN_PATTERN.fullmatch(value) is not None:
        return value
    return None


def _set_cookies(response: Response, raw_session: str, max_age: int) -> None:
    secure = get_settings().cookie_secure
    response.set_cookie(
        SESSION_COOKIE,
        raw_session,
        max_age=max_age,
        path=COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        XSRF_COOKIE,
        _xsrf_for(raw_session),
        max_age=max_age,
        path=COOKIE_PATH,
        secure=secure,
        httponly=False,  # read by the Angular client to fill X-XSRF-TOKEN
        samesite="lax",
    )


def _clear_cookies(response: Response) -> None:
    secure = get_settings().cookie_secure
    response.delete_cookie(
        SESSION_COOKIE, path=COOKIE_PATH, secure=secure, httponly=True, samesite="lax"
    )
    response.delete_cookie(
        XSRF_COOKIE, path=COOKIE_PATH, secure=secure, httponly=False, samesite="lax"
    )


def create_auth_session(db: Session, user: User, response: Response) -> None:
    """Open a new session for `user` and set the session and XSRF cookies on `response`."""
    ttl = timedelta(hours=get_settings().session_ttl_hours)
    raw = new_secret()
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=hash_secret(raw),
            expires_at=datetime.now(UTC) + ttl,
        )
    )
    db.flush()
    _set_cookies(response, raw, int(ttl.total_seconds()))
    log_event("auth.session_created")


def revoke_auth_session(db: Session, request: Request, response: Response) -> None:
    """Revoke the session identified by the request cookie (if any) and clear the cookies."""
    raw = _valid_session_token(request.cookies.get(SESSION_COOKIE))
    if raw is not None:
        result = db.execute(
            update(AuthSession)
            .where(AuthSession.token_hash == hash_secret(raw), AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        db.flush()
        log_event("auth.session_revoked", revoked=bool(getattr(result, "rowcount", 0)))
    _clear_cookies(response)


def revoke_all_sessions(db: Session, user_id: UUID) -> None:
    """Revoke every active session of `user_id` (e.g. after a password reset)."""
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    db.flush()
    log_event("auth.sessions_revoked_all")


def _check_csrf(request: Request, raw_session: str) -> None:
    if request.method.upper() in SAFE_METHODS:
        return
    header = request.headers.get(XSRF_HEADER)
    cookie = request.cookies.get(XSRF_COOKIE)
    if not isinstance(header, str) or _XSRF_TOKEN_PATTERN.fullmatch(header) is None:
        raise _csrf_failed("header_missing_or_malformed")
    if not isinstance(cookie, str) or _XSRF_TOKEN_PATTERN.fullmatch(cookie) is None:
        raise _csrf_failed("cookie_missing_or_malformed")
    if not hmac.compare_digest(header, cookie):
        raise _csrf_failed("mismatch")
    if not hmac.compare_digest(header, _xsrf_for(raw_session)):
        raise _csrf_failed("not_bound_to_session")


def _authenticate(request: Request, db: Session) -> User:
    """Resolve the session owner or raise AUTH_REQUIRED / CSRF_FAILED."""
    raw = _valid_session_token(request.cookies.get(SESSION_COOKIE))
    if raw is None:
        raise _auth_required("missing_or_malformed")

    row = db.execute(
        select(AuthSession, User)
        .join(User, User.id == AuthSession.user_id)
        .where(AuthSession.token_hash == hash_secret(raw))
    ).one_or_none()
    if row is None:
        raise _auth_required("unknown")
    auth_session, user = row
    if auth_session.revoked_at is not None:
        raise _auth_required("revoked")
    if auth_session.expires_at <= datetime.now(UTC):
        raise _auth_required("expired")
    if user.deletion_requested_at is not None:
        raise _auth_required("deletion_requested")

    _check_csrf(request, raw)
    return user


def _has_current_acceptance(user: User) -> bool:
    settings = get_settings()
    return (
        user.terms_accepted_at is not None
        and user.terms_version == settings.terms_version
        and user.privacy_version == settings.privacy_version
    )


def get_current_user_pending_terms(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> User:
    """Authenticated user, even without a current terms acceptance (e.g. to accept them)."""
    return _authenticate(request, db)


def get_current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    """Authenticated user with a current acceptance of the terms and privacy policy.

    Raises 401 AUTH_REQUIRED, 403 CSRF_FAILED or 403 TERMS_REQUIRED.
    """
    user = _authenticate(request, db)
    if not _has_current_acceptance(user):
        log_event("auth.terms_required")
        raise AppError.from_catalog(TERMS_REQUIRED)
    return user
