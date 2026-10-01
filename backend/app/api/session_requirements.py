"""Job requirements and plan routes (plan section 8.1, "Sessions"; AUTH-16, PLAN-03, PLAN-04,
PLAN-05, PLAN-07, PLAN-08, PLAN-09, PLAN-10, PLAN-15, PLAN-90, PLAN-92).

Thin HTTP layer over CT-37 (requirements structuring), CT-38 (list editing and confirmation),
CT-39 (preparation retry) and CT-40 (``SessionView``). The modules flush; these routes own the
transaction and commit explicitly (CT-2). Anything left uncommitted when an error is raised is
rolled back by ``get_db``. Question preparation is never run here: ``confirm_plan`` and
``retry_preparation`` only enqueue the job.

Every route depends on ``CurrentUser`` (session, CSRF, current terms) and resolves the session
through ``get_owned_session``: a session of another user, an unknown id and a malformed id all
get the same 404 ``RESOURCE_NOT_FOUND`` (AUTH-16), before the LLM client is even built.

Locking: the requirements route reads the session without ``FOR UPDATE`` so no row lock is held
while the LLM runs; ``structure_requirements`` locks and re-checks the row itself afterwards.
The other routes lock the row up front.

Body cap: the requirements text may hold up to ``MAX_REQUIREMENTS_CHARS`` (20,000) characters,
which does not fit the 16 KiB cap of the auth routes (LAC-32) once UTF-8 and JSON escaping are
counted. These routes use their own cap, ``MAX_REQUIREMENTS_BODY_BYTES``, rejected with
``VALIDATION_ERROR`` before parsing. Longer text within the cap is truncated by the service.
"""

import re
from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, StrictStr
from sqlalchemy.orm import Session
from starlette.types import Message, Receive

from app.api.deps import CurrentUser, DbSession
from app.errors import LLM_UNAVAILABLE, RESOURCE_NOT_FOUND, VALIDATION_ERROR, AppError
from app.interviews.question_generation import retry_preparation
from app.interviews.requirement_list import (
    RequirementItemInput,
    confirm_plan,
    confirm_requirement_list,
    replace_requirement_list,
)
from app.interviews.requirements import structure_requirements
from app.interviews.sessions import get_owned_session
from app.interviews.views import SessionView, build_session_view
from app.llm.client import LLMClient, get_llm_client
from app.models.account import User
from app.models.interview import InterviewSession
from app.observability import log_event

# 20,000 characters take at most 80 KB in UTF-8 (about 120 KB as JSON \u escapes in the worst
# case); 128 KiB fits a full-length text with any encoding while staying a hard bound.
MAX_REQUIREMENTS_BODY_BYTES = 128 * 1024

_CONTENT_LENGTH = re.compile(r"[0-9]{1,20}")

LLMFactory = Callable[[], LLMClient]


def _body_too_large() -> AppError:
    return AppError.from_catalog(
        VALIDATION_ERROR,
        details={
            "fields": [{"loc": ["body"], "msg": "Request body too large.", "type": "value_error"}]
        },
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


class RequirementsBodyRoute(APIRoute):
    """Route that refuses bodies above ``MAX_REQUIREMENTS_BODY_BYTES`` before parsing them.

    The declared ``Content-Length`` is checked first; the stream is then read with a running
    total, so a chunked body without a length cannot bypass the limit.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def bounded_handler(request: Request) -> Response:
            declared = request.headers.get("content-length")
            if declared is not None and (
                _CONTENT_LENGTH.fullmatch(declared) is None
                or int(declared) > MAX_REQUIREMENTS_BODY_BYTES
            ):
                raise _body_too_large()
            received = bytearray()
            async for chunk in request.stream():
                received.extend(chunk)
                if len(received) > MAX_REQUIREMENTS_BODY_BYTES:
                    raise _body_too_large()
            replayed = Request(request.scope, _replayed_receive(bytes(received), request.receive))
            return await handler(replayed)

        return bounded_handler


def get_llm_factory() -> LLMFactory:
    """FastAPI dependency: how to build the LLM client, called only after the ownership check."""
    return get_llm_client


LLMClientFactory = Annotated[LLMFactory, Depends(get_llm_factory)]


class RequirementsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Sanitized, stripped and truncated to MAX_REQUIREMENTS_CHARS by structure_requirements.
    text: StrictStr


class RequirementListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RequirementItemInput]


router = APIRouter(prefix="/api", tags=["sessions"], route_class=RequirementsBodyRoute)


def _owned(db: Session, user: User, session_id: str, for_update: bool) -> InterviewSession:
    """Resolve a path id into an owned session; malformed ids get the same 404 (AUTH-16)."""
    try:
        parsed = UUID(session_id)
    except ValueError:
        raise AppError.from_catalog(RESOURCE_NOT_FOUND) from None
    return get_owned_session(db, user, parsed, for_update=for_update)


def _build_llm(factory: LLMFactory) -> LLMClient:
    try:
        return factory()
    except ValueError as error:
        # Misconfigured inference server (e.g. host outside llm_allowed_hosts).
        log_event("requirements.llm_client_unavailable", reason="invalid_configuration")
        raise AppError.from_catalog(LLM_UNAVAILABLE) from error


def _view_and_commit(db: Session, session: InterviewSession) -> SessionView:
    view = build_session_view(db, session)
    db.commit()
    return view


@router.post("/sessions/{session_id}/requirements")
def submit_requirements(
    session_id: str,
    body: RequirementsRequest,
    user: CurrentUser,
    db: DbSession,
    llm_factory: LLMClientFactory,
) -> SessionView:
    # No FOR UPDATE here: structure_requirements relocks after the LLM call (PR-4).
    session = _owned(db, user, session_id, for_update=False)
    structure_requirements(db, _build_llm(llm_factory), session, body.text)
    return _view_and_commit(db, session)


@router.put("/sessions/{session_id}/requirement-list")
def edit_requirement_list(
    session_id: str, body: RequirementListRequest, user: CurrentUser, db: DbSession
) -> SessionView:
    session = _owned(db, user, session_id, for_update=True)
    replace_requirement_list(db, session, body.items)
    return _view_and_commit(db, session)


@router.post("/sessions/{session_id}/requirement-list/confirm")
def confirm_list(session_id: str, user: CurrentUser, db: DbSession) -> SessionView:
    session = _owned(db, user, session_id, for_update=True)
    # Confirmation errors come back as catalog AppErrors (e.g. TOO_MANY_REQUIRED_SKILLS with
    # count and excess); the proposal itself is part of the SessionView.
    confirm_requirement_list(db, session)
    return _view_and_commit(db, session)


@router.post("/sessions/{session_id}/plan/confirm")
def confirm_interview_plan(session_id: str, user: CurrentUser, db: DbSession) -> SessionView:
    session = _owned(db, user, session_id, for_update=True)
    confirm_plan(db, session)
    return _view_and_commit(db, session)


@router.post("/sessions/{session_id}/preparation/retry")
def retry_question_preparation(session_id: str, user: CurrentUser, db: DbSession) -> SessionView:
    session = _owned(db, user, session_id, for_update=True)
    retry_preparation(db, session)
    return _view_and_commit(db, session)


__all__ = [
    "MAX_REQUIREMENTS_BODY_BYTES",
    "RequirementListRequest",
    "RequirementsBodyRoute",
    "RequirementsRequest",
    "get_llm_factory",
    "router",
]
