"""Google sign-in routes (plan section 8.1, "Google e conta"; AS-1, AS-6).

Thin HTTP layer over CT-17 (OIDC client), CT-18 (account resolution and linking) and CT-11
(sessions). The routes own the transaction and commit explicitly (CT-2).

``GOOGLE_AUTH_FAILED`` never reaches the global error handler here: the start and callback
routes turn it into a redirect to ``{frontend}/login?error=google_failed`` after rolling back
anything left pending, so no account is created or linked (AUTH-93). Every callback answer
expires the OIDC state cookie.

``POST /api/auth/google/link`` checks a local password, so besides the per-account throttle of
``complete_link`` it also counts failures per origin, like the local login (DA-5). The origin
row is never held while ``complete_link`` runs: that function locks the user row and then the
account row, and the local login locks account then origin rows, so holding the origin row
across the call could close a lock cycle. The origin check and the failure record therefore
run in their own short transactions. Tokens, passwords and e-mails are never logged.
"""

from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth_local import (
    BoundedBodyRoute,
    BoundedStr,
    LoginResponse,
    _client_key,
    _me,
    _revoke_received_session,
)
from app.api.deps import DbSession
from app.auth import throttle
from app.auth.google_accounts import complete_link, resolve_google_login
from app.auth.google_oidc import build_authorization_redirect, clear_state_cookie, exchange_callback
from app.auth.sessions import create_auth_session, revoke_auth_session
from app.config import get_settings
from app.errors import INVALID_CREDENTIALS, TOO_MANY_ATTEMPTS, AppError
from app.observability import log_event

router = APIRouter(prefix="/api/auth/google", tags=["auth"], route_class=BoundedBodyRoute)

_KIND_PATHS = {"signed_in": "/resumes", "needs_terms": "/accept-terms"}


class LinkGoogleRequest(BaseModel):
    token: BoundedStr
    password: BoundedStr


def _frontend_url(path: str, query: dict[str, str] | None = None) -> str:
    base = get_settings().frontend_base_url.rstrip("/")
    return f"{base}{path}?{urlencode(query)}" if query else f"{base}{path}"


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _login_failed_redirect() -> RedirectResponse:
    response = RedirectResponse(_frontend_url("/login", {"error": "google_failed"}), 302)
    _no_store(response)
    return response


@router.get("/start")
def start(request: Request) -> Response:
    try:
        response: Response = build_authorization_redirect(request)
    except AppError:
        return _login_failed_redirect()
    return _no_store(response)


@router.get("/callback")
def callback(request: Request, db: DbSession) -> Response:
    try:
        identity = exchange_callback(request)
        result = resolve_google_login(db, identity)
    except AppError:
        # AUTH-93: nothing created or linked survives a failed sign-in.
        db.rollback()
        response = _login_failed_redirect()
        clear_state_cookie(response)
        return response

    if result.kind == "needs_link":
        # AUTH-11: no session before the local password is confirmed.
        token = result.pending_link_token or ""
        db.commit()
        response = RedirectResponse(_frontend_url("/link-google", {"token": token}), 302)
        # The token travels in the URL: keep it out of Referer headers.
        response.headers["Referrer-Policy"] = "no-referrer"
    else:
        user = result.user
        if user is None:  # pragma: no cover - CT-18 always returns a user for these kinds
            db.rollback()
            response = _login_failed_redirect()
            clear_state_cookie(response)
            return response
        response = RedirectResponse(_frontend_url(_KIND_PATHS[result.kind]), 302)
        # GET is CSRF-exempt (CT-11), so the received session is always revoked here.
        revoke_auth_session(db, request, Response())
        create_auth_session(db, user, response)
        db.commit()
    clear_state_cookie(response)
    return _no_store(response)


def _check_origin(db: Session, origin: str) -> None:
    entries = throttle.acquire(db, [origin])
    locked = throttle.is_locked(entries)
    # Release the origin row before complete_link takes the user and account rows.
    db.commit()
    if locked:
        log_event("auth.google_link_rejected", reason="origin_locked")
        raise AppError.from_catalog(TOO_MANY_ATTEMPTS)


def _record_origin_failure(db: Session, origin: str) -> None:
    # complete_link has already committed its own failure; this runs in a fresh transaction.
    throttle.record_failure(db, throttle.acquire(db, [origin]))
    db.commit()


@router.post("/link")
def link(
    body: LinkGoogleRequest, request: Request, response: Response, db: DbSession
) -> LoginResponse:
    origin = throttle.origin_key(_client_key(request))
    _check_origin(db, origin)
    # Nothing may be pending before complete_link: it commits on the wrong-password path.
    try:
        user = complete_link(db, body.token, body.password)
    except AppError as error:
        if error.code == INVALID_CREDENTIALS:
            _record_origin_failure(db, origin)
        raise
    _revoke_received_session(db, request)
    create_auth_session(db, user, response)
    db.commit()
    return LoginResponse(user=_me(user))


__all__ = ["router"]
