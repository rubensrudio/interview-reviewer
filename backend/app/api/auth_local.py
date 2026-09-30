"""Local authentication routes (plan section 8.1, "Auth local"; AS-1, AS-6).

Thin HTTP layer over CT-14 (registration), CT-15 (login), CT-16 (password reset) and CT-11
(sessions). The modules flush; these routes own the transaction and commit explicitly (CT-2).
Anything left uncommitted when an error is raised is rolled back by ``get_db``.

Answers that could reveal whether an account exists (register, resend-verification,
forgot-password) always carry the same neutral body. Every request body is capped at
``MAX_BODY_BYTES`` and every string field at ``MAX_FIELD_CHARS`` (LAC-32), both rejected with
``VALIDATION_ERROR`` before any lookup, so the limits never depend on the account. E-mails,
passwords and tokens are never logged here.
"""

import re
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, StrictBool, StrictStr
from starlette.types import Message, Receive

from app.api.deps import CurrentUserPendingTerms, DbSession
from app.auth.login import login_local
from app.auth.password_reset import request_password_reset, reset_password
from app.auth.registration import register_local, resend_verification, verify_email
from app.auth.sessions import create_auth_session, revoke_auth_session
from app.errors import VALIDATION_ERROR, AppError
from app.legal.consent import has_current_consent
from app.models.account import User

# LAC-32: generous for real passphrases, small enough to keep Argon2 and parsing cheap.
MAX_FIELD_CHARS = 1024
MAX_BODY_BYTES = 16 * 1024

REGISTER_MESSAGE = (
    "Check your inbox to continue. If you already have an account, sign in or reset your password."
)
# Spec section 9 has no text for resend; the neutral registration text is reused on purpose.
RESEND_MESSAGE = REGISTER_MESSAGE
RESET_REQUESTED_MESSAGE = (
    "If an account exists for this e-mail, we sent instructions to reset your password."
)
EMAIL_VERIFIED_MESSAGE = "Your e-mail has been verified. You can now sign in."  # LAC-31

_CONTENT_LENGTH = re.compile(r"[0-9]{1,20}")

BoundedStr = Annotated[StrictStr, Field(max_length=MAX_FIELD_CHARS)]


def _body_rejected() -> AppError:
    return AppError.from_catalog(
        VALIDATION_ERROR,
        details={
            "fields": [{"loc": ["body"], "msg": "Request body too large.", "type": "value_error"}]
        },
    )


def _replay(body: bytes, downstream: Receive) -> Receive:
    """Receive callable that yields the already-read body once, then defers to the server."""
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await downstream()

    return receive


class BoundedBodyRoute(APIRoute):
    """Route that refuses request bodies above ``MAX_BODY_BYTES`` before they are parsed.

    The declared ``Content-Length`` is checked first; the stream is then read with a running
    total, so a chunked body without a length cannot bypass the limit.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def bounded_handler(request: Request) -> Response:
            declared = request.headers.get("content-length")
            if declared is not None and (
                _CONTENT_LENGTH.fullmatch(declared) is None or int(declared) > MAX_BODY_BYTES
            ):
                raise _body_rejected()
            received = bytearray()
            async for chunk in request.stream():
                received.extend(chunk)
                if len(received) > MAX_BODY_BYTES:
                    raise _body_rejected()
            replayed = Request(request.scope, _replay(bytes(received), request.receive))
            return await handler(replayed)

        return bounded_handler


router = APIRouter(prefix="/api/auth", tags=["auth"], route_class=BoundedBodyRoute)


class RegisterRequest(BaseModel):
    email: BoundedStr
    password: BoundedStr
    # Missing means "not accepted"; strict so "yes" or 1 is never read as consent.
    accept_terms: StrictBool = False


class RegisterResponse(BaseModel):
    message: str
    email_delivery: Literal["sent", "delayed"]


class EmailRequest(BaseModel):
    email: BoundedStr


class TokenRequest(BaseModel):
    token: BoundedStr


class LoginRequest(BaseModel):
    email: BoundedStr
    password: BoundedStr


class ResetPasswordRequest(BaseModel):
    token: BoundedStr
    new_password: BoundedStr


class MessageResponse(BaseModel):
    message: str


class Me(BaseModel):
    id: UUID
    email: str
    has_password: bool
    google_linked: bool
    terms_accepted: bool


class LoginResponse(BaseModel):
    user: Me


def _me(user: User) -> Me:
    return Me(
        id=user.id,
        email=user.email_normalized,
        has_password=user.password_hash is not None,
        google_linked=user.google_sub is not None,
        terms_accepted=has_current_consent(user),
    )


def _client_key(request: Request) -> str:
    # Raw peer address; the throttle keys it with an HMAC (CT-15). Proxy headers are not
    # trusted here: the server must be configured to resolve the real client address.
    return request.client.host if request.client is not None else ""


@router.post("/register", status_code=202)
def register(body: RegisterRequest, db: DbSession) -> RegisterResponse:
    dispatch = register_local(db, body.email, body.password, body.accept_terms)
    db.commit()
    # "skipped" never reaches the client: it must look like a sent e-mail (AUTH-02).
    delivery: Literal["sent", "delayed"] = "delayed" if dispatch == "delayed" else "sent"
    return RegisterResponse(message=REGISTER_MESSAGE, email_delivery=delivery)


@router.post("/verify-email")
def verify(body: TokenRequest, db: DbSession) -> MessageResponse:
    verify_email(db, body.token)
    db.commit()
    return MessageResponse(message=EMAIL_VERIFIED_MESSAGE)


@router.post("/resend-verification", status_code=202)
def resend(body: EmailRequest, db: DbSession) -> MessageResponse:
    # "sent", "delayed" and "skipped" all get the same answer (AUTH-02, AUTH-92).
    resend_verification(db, body.email)
    db.commit()
    return MessageResponse(message=RESEND_MESSAGE)


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response, db: DbSession) -> LoginResponse:
    # Nothing may be pending before login_local: it commits on the failure path (CT-15).
    user = login_local(db, body.email, body.password, _client_key(request))
    # Rotate: the session of the received cookie (if any) is revoked before a new one is
    # issued. Its cookie-clearing headers are discarded; the new cookies replace them.
    revoke_auth_session(db, request, Response())
    create_auth_session(db, user, response)
    db.commit()
    return LoginResponse(user=_me(user))


@router.post("/logout", status_code=204, response_class=Response)
def logout(request: Request, response: Response, db: DbSession) -> None:
    # CT-11 applies the CSRF check whenever the cookie is well formed, active or not.
    revoke_auth_session(db, request, response)
    db.commit()


@router.get("/me")
def me(user: CurrentUserPendingTerms) -> Me:
    return _me(user)


@router.post("/forgot-password", status_code=202)
def forgot_password(body: EmailRequest, db: DbSession) -> MessageResponse:
    request_password_reset(db, body.email)
    db.commit()
    return MessageResponse(message=RESET_REQUESTED_MESSAGE)


@router.post("/reset-password", status_code=204, response_class=Response)
def reset(body: ResetPasswordRequest, db: DbSession) -> None:
    # LINK_INVALID, PASSWORD_POLICY and VALIDATION_ERROR propagate as catalog errors.
    reset_password(db, body.token, body.new_password)
    db.commit()


__all__ = ["router"]
