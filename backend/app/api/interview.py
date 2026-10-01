"""Interview routes: answers, clarifications and evaluation retry (plan section 8.1, "Interview";
AUTH-16, INTV-03, INTV-04, INTV-05, INTV-06, INTV-08, INTV-90, INTV-91, INTV-92, INTV-93,
EVAL-14).

Thin HTTP layer over CT-41 (``submit_answer``), CT-42 (``request_clarification``), CT-48
(``retry_evaluation``), CT-36 (``get_owned_session``) and CT-40 (``SessionView``). The modules
flush; these routes own the transaction and commit explicitly (CT-2). Anything left uncommitted
when an error is raised is rolled back by ``get_db``. Answers are immutable: there is no route
to edit one.

Every route depends on ``CurrentUser`` (session, CSRF, current terms) and resolves the session
through ``get_owned_session``: a session of another user, an unknown id and a malformed id all
get the same 404 ``RESOURCE_NOT_FOUND`` (AUTH-16), before the LLM client is even built.

Locking: ``submit_answer`` and ``request_clarification`` lock the session row themselves, so
the session is read here without ``FOR UPDATE``; in particular no row lock is held while the
LLM answers a clarification. The evaluation retry locks the row up front.

Idempotency: ``POST /answers`` requires the ``Idempotency-Key`` header (missing: 422). The key
must be 1 to 64 visible ASCII characters (``!`` to ``~``); anything else is a
``VALIDATION_ERROR`` before the session is read.

Clarification failures: when ``CLARIFICATION_UNAVAILABLE`` (503) is raised nothing is
committed, so the candidate's request is not kept either; the interview state is unchanged and
answering keeps working (INTV-92).

Body cap: an answer may hold ``max_answer_chars`` characters, which does not fit the 16 KiB cap
of the auth routes (LAC-32) once UTF-8 and JSON escaping are counted. These routes use their own
cap derived from the setting (``max_answer_body_bytes``), rejected with ``VALIDATION_ERROR``
before parsing.
"""

import re
from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, StrictStr
from sqlalchemy.orm import Session
from starlette.types import Message, Receive

from app.api.deps import CurrentUser, DbSession
from app.api.session_requirements import LLMClientFactory
from app.config import get_settings
from app.errors import (
    CLARIFICATION_UNAVAILABLE,
    NOT_CURRENT_QUESTION,
    QUESTION_ALREADY_ANSWERED,
    RESOURCE_NOT_FOUND,
    VALIDATION_ERROR,
    AppError,
)
from app.evaluation.pipeline import retry_evaluation
from app.interviews.answers import IDEMPOTENCY_KEY_MAX_LENGTH, submit_answer
from app.interviews.clarification import request_clarification
from app.interviews.sessions import get_owned_session
from app.interviews.views import MessageView, SessionView, build_session_view
from app.llm.client import LLMClient
from app.models.account import User
from app.models.interview import InterviewSession
from app.observability import log_event

# Worst case per character as JSON: an astral character escaped as two \uXXXX sequences.
_JSON_BYTES_PER_CHAR = 12
# Room for the JSON keys, the question id and whitespace around the text.
_BODY_OVERHEAD_BYTES = 4096

_CONTENT_LENGTH = re.compile(r"[0-9]{1,20}")
_IDEMPOTENCY_KEY = re.compile(rf"[!-~]{{1,{IDEMPOTENCY_KEY_MAX_LENGTH}}}")

# Errors that come back with the current SessionView so a stale tab can catch up (INTV-90/91).
_STALE_TAB_ERRORS = frozenset({QUESTION_ALREADY_ANSWERED, NOT_CURRENT_QUESTION})


def max_answer_body_bytes() -> int:
    """Body cap that fits an answer of ``max_answer_chars`` with any encoding."""
    return get_settings().max_answer_chars * _JSON_BYTES_PER_CHAR + _BODY_OVERHEAD_BYTES


def _validation_error(loc: list[str], msg: str) -> AppError:
    return AppError.from_catalog(
        VALIDATION_ERROR, details={"fields": [{"loc": loc, "msg": msg, "type": "value_error"}]}
    )


def _replayed_receive(body: bytes, downstream: Receive) -> Receive:
    """Receive callable that yields the already-read body once, then defers to the server."""
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await downstream()

    return receive


class AnswerBodyRoute(APIRoute):
    """Route that refuses bodies above ``max_answer_body_bytes()`` before parsing them.

    The declared ``Content-Length`` is checked first; the stream is then read with a running
    total, so a chunked body without a length cannot bypass the limit.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def bounded_handler(request: Request) -> Response:
            limit = max_answer_body_bytes()
            declared = request.headers.get("content-length")
            if declared is not None and (
                _CONTENT_LENGTH.fullmatch(declared) is None or int(declared) > limit
            ):
                raise _validation_error(["body"], "Request body too large.")
            received = bytearray()
            async for chunk in request.stream():
                received.extend(chunk)
                if len(received) > limit:
                    raise _validation_error(["body"], "Request body too large.")
            replayed = Request(request.scope, _replayed_receive(bytes(received), request.receive))
            return await handler(replayed)

        return bounded_handler


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: UUID
    # Sanitized and checked (blank, length) by submit_answer.
    content: StrictStr


class ClarificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Sanitized and checked (blank, length) by request_clarification.
    text: StrictStr


class ClarificationResponse(BaseModel):
    message: MessageView


router = APIRouter(prefix="/api", tags=["interview"], route_class=AnswerBodyRoute)


def _owned(db: Session, user: User, session_id: str, for_update: bool) -> InterviewSession:
    """Resolve a path id into an owned session; malformed ids get the same 404 (AUTH-16)."""
    try:
        parsed = UUID(session_id)
    except ValueError:
        raise AppError.from_catalog(RESOURCE_NOT_FOUND) from None
    return get_owned_session(db, user, parsed, for_update=for_update)


def _idempotency_key(value: object) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise _validation_error(
            ["header", "Idempotency-Key"],
            f"Must be 1 to {IDEMPOTENCY_KEY_MAX_LENGTH} visible ASCII characters.",
        )
    return value


def _build_llm(factory: Callable[[], LLMClient]) -> LLMClient:
    try:
        return factory()
    except ValueError as error:
        # Misconfigured inference server (e.g. host outside llm_allowed_hosts).
        log_event("clarification.llm_client_unavailable", reason="invalid_configuration")
        raise AppError.from_catalog(CLARIFICATION_UNAVAILABLE) from error


@router.post("/sessions/{session_id}/answers")
def answer(
    session_id: str,
    body: AnswerRequest,
    user: CurrentUser,
    db: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> SessionView:
    key = _idempotency_key(idempotency_key)
    # No FOR UPDATE here: submit_answer locks the row before reading anything else.
    session = _owned(db, user, session_id, for_update=False)
    try:
        submit_answer(db, session, body.question_id, body.content, key)
    except AppError as error:
        if error.code not in _STALE_TAB_ERRORS:
            raise
        # Nothing was written; show the stale tab where the interview is (INTV-90, INTV-91).
        view = build_session_view(db, session)
        raise AppError.from_catalog(
            error.code, details={"session": view.model_dump(mode="json")}
        ) from None
    view = build_session_view(db, session)
    db.commit()
    return view


@router.post("/sessions/{session_id}/clarifications")
def clarify(
    session_id: str,
    body: ClarificationRequest,
    user: CurrentUser,
    db: DbSession,
    llm_factory: LLMClientFactory,
) -> ClarificationResponse:
    # No FOR UPDATE here: request_clarification relocks after the LLM call.
    session = _owned(db, user, session_id, for_update=False)
    message = request_clarification(db, _build_llm(llm_factory), session, body.text)
    response = ClarificationResponse(
        message=MessageView(
            id=message.id,
            role=message.role,
            kind=message.kind,
            content=message.content,
            created_at=message.created_at,
        )
    )
    db.commit()
    return response


@router.post("/sessions/{session_id}/evaluation/retry")
def retry(session_id: str, user: CurrentUser, db: DbSession) -> SessionView:
    session = _owned(db, user, session_id, for_update=True)
    retry_evaluation(db, session)
    view = build_session_view(db, session)
    db.commit()
    return view


__all__ = [
    "AnswerBodyRoute",
    "AnswerRequest",
    "ClarificationRequest",
    "ClarificationResponse",
    "max_answer_body_bytes",
    "router",
]
