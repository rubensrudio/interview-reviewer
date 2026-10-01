"""Interview session routes (plan section 8.1, "Sessions"; AUTH-16, PLAN-01, PLAN-02, INTV-10,
INTV-12, DATA-01, DATA-92, LANG-01).

Thin HTTP layer over CT-36 (session lifecycle) and CT-40 (``SessionView``). The modules flush;
these routes own the transaction and commit explicitly (CT-2). Anything left uncommitted when
an error is raised is rolled back by ``get_db``.

Every route depends on ``CurrentUser`` (session, CSRF on non-GET methods, current terms).
Every route that reads one session goes through ``get_owned_session``: a session of another
user, an unknown id and a malformed id all get the same 404 ``RESOURCE_NOT_FOUND`` without any
resource data (AUTH-16). Reading a non-terminal session counts as candidate activity.

Request bodies keep the 16 KiB cap of LAC-32 (``BoundedBodyRoute``).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError
from sqlalchemy.orm import Session

from app.api.auth_local import BoundedBodyRoute
from app.api.deps import CurrentUser, DbSession
from app.errors import RESOURCE_NOT_FOUND, AppError
from app.interviews.sessions import (
    cancel_session,
    get_owned_session,
    list_sessions,
    start_session,
    supported_languages,
    touch_activity,
)
from app.interviews.state_machine import TERMINAL_STATUSES
from app.interviews.views import SessionView, build_session_view
from app.models.account import User
from app.models.interview import ExpectedLevel, InterviewSession, RequirementItem, SessionStatus
from app.observability import log_event

# Display labels of the interview languages (spec section 9; LAC-08: English only).
LANGUAGE_LABELS: dict[str, str] = {"en": "English"}

REQUIRED = "required"


class LanguageOption(BaseModel):
    code: str
    label: str


class InterviewOptions(BaseModel):
    languages: list[LanguageOption]
    levels: list[ExpectedLevel]


class StartSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resume_id: UUID
    # Checked by start_session against the approved languages (LANGUAGE_NOT_SUPPORTED).
    language: StrictStr
    interview_level: ExpectedLevel | None = None


class SessionSummary(BaseModel):
    id: UUID
    created_at: datetime
    status: SessionStatus
    resume_name: str | None
    required_skills: list[str]
    completed_at: datetime | None


router = APIRouter(prefix="/api", tags=["sessions"], route_class=BoundedBodyRoute)


# --- helpers ---------------------------------------------------------------------------------


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str)]


def _frozen_required_skills(session: InterviewSession) -> list[str]:
    skills: list[str] = []
    for entry in session.requirement_items or []:
        try:
            item = RequirementItem.model_validate(entry)
        except ValidationError:
            # A malformed stored entry is skipped instead of failing the whole history.
            log_event("session.requirement_item_invalid", session_id=str(session.id))
            continue
        if item.classification == REQUIRED:
            skills.append(item.name)
    return skills


def _required_skills(session: InterviewSession) -> list[str]:
    """Confirmed required skills only (DATA-01); a draft list is not shown in the history."""
    if isinstance(session.proposal, dict):
        return _string_list(session.proposal.get("skills"))
    if session.planned_count is not None:
        return _frozen_required_skills(session)
    return []


def _summary(session: InterviewSession) -> SessionSummary:
    return SessionSummary(
        id=session.id,
        created_at=session.created_at,
        status=session.status,
        resume_name=session.resume_name,
        required_skills=_required_skills(session),
        completed_at=session.completed_at,
    )


def _owned(db: Session, user: User, session_id: str, for_update: bool = False) -> InterviewSession:
    """Resolve a path id into an owned session; malformed ids get the same 404 (AUTH-16)."""
    try:
        parsed = UUID(session_id)
    except ValueError:
        raise AppError.from_catalog(RESOURCE_NOT_FOUND) from None
    return get_owned_session(db, user, parsed, for_update=for_update)


# --- routes ----------------------------------------------------------------------------------


@router.get("/interview-options")
def interview_options(user: CurrentUser) -> InterviewOptions:
    languages = [
        LanguageOption(code=code, label=LANGUAGE_LABELS.get(code, code))
        for code in supported_languages()
    ]
    return InterviewOptions(languages=languages, levels=list(ExpectedLevel))


@router.post("/sessions", status_code=201)
def start(body: StartSessionRequest, user: CurrentUser, db: DbSession) -> SessionView:
    session = start_session(db, user, body.resume_id, body.language, body.interview_level)
    view = build_session_view(db, session)
    db.commit()
    return view


@router.get("/sessions")
def list_own(user: CurrentUser, db: DbSession) -> list[SessionSummary]:
    return [_summary(session) for session in list_sessions(db, user)]


@router.get("/sessions/{session_id}")
def get_one(session_id: str, user: CurrentUser, db: DbSession) -> SessionView:
    session = _owned(db, user, session_id)
    view = build_session_view(db, session)
    if session.status not in TERMINAL_STATUSES:
        # Returning to an open session is candidate activity (INTV-10, INTV-13).
        touch_activity(session)
        db.commit()
    return view


@router.post("/sessions/{session_id}/cancel")
def cancel(session_id: str, user: CurrentUser, db: DbSession) -> SessionView:
    session = _owned(db, user, session_id, for_update=True)
    cancel_session(db, session)
    view = build_session_view(db, session)
    db.commit()
    return view


__all__ = ["router"]
