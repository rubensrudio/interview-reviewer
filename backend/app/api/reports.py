"""Report route (plan section 8.1, "Reports"; DATA-02, EVAL-08, EVAL-12, INTV-14, AUTH-16).

Thin read-only HTTP layer over CT-34 (``Report``) and CT-36 (``get_owned_session``). The route
returns the frozen ``reports.content`` exactly as it was stored when the session completed: it
never rebuilds, recalculates or rewrites the report (EVAL-12, DATA-02).

A session of another user, an unknown id and a malformed id all get the same 404
``RESOURCE_NOT_FOUND`` without any resource data (AUTH-16). A session that is not ``completed``
gets 409 ``REPORT_NOT_AVAILABLE`` and no report data at all, so no reference point or expected
answer leaks before completion (INTV-14).
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.errors import REPORT_NOT_AVAILABLE, RESOURCE_NOT_FOUND, AppError
from app.interviews.sessions import get_owned_session
from app.models.assessment import Report
from app.models.interview import SessionStatus
from app.reports.builder import ReportContent
from app.reports.pdf_export import render_report_pdf

router = APIRouter(prefix="/api", tags=["reports"])


@router.get("/sessions/{session_id}/report", response_model=None)
def get_report(session_id: str, user: CurrentUser, db: DbSession) -> JSONResponse:
    try:
        parsed = UUID(session_id)
    except ValueError:
        # Malformed ids get the same 404 as unknown ones and sessions of other users (AUTH-16).
        raise AppError.from_catalog(RESOURCE_NOT_FOUND) from None
    session = get_owned_session(db, user, parsed)
    if session.status != SessionStatus.COMPLETED:
        raise AppError.from_catalog(REPORT_NOT_AVAILABLE)
    content: dict[str, Any] | None = db.scalar(
        select(Report.content).where(Report.session_id == session.id)
    )
    if content is None:
        raise AppError.from_catalog(REPORT_NOT_AVAILABLE)
    # The stored JSON goes out untouched: no model round trip that could reshape it (EVAL-12).
    return JSONResponse(content=content)


@router.get("/sessions/{session_id}/report.pdf", response_model=None)
def export_report_pdf(session_id: str, user: CurrentUser, db: DbSession) -> Response:
    """Export the frozen report as a PDF (EXPT-01); only for a completed session (EXPT-02)."""
    try:
        parsed = UUID(session_id)
    except ValueError:
        # Same uniform 404 as the JSON report route (AUTH-16).
        raise AppError.from_catalog(RESOURCE_NOT_FOUND) from None
    session = get_owned_session(db, user, parsed)
    if session.status != SessionStatus.COMPLETED:
        raise AppError.from_catalog(REPORT_NOT_AVAILABLE)
    stored: dict[str, Any] | None = db.scalar(
        select(Report.content).where(Report.session_id == session.id)
    )
    if stored is None:
        raise AppError.from_catalog(REPORT_NOT_AVAILABLE)
    try:
        content = ReportContent.model_validate(stored)
    except ValidationError:
        # A stored report that does not match the schema cannot be rendered; no detail leaks.
        raise AppError.from_catalog(REPORT_NOT_AVAILABLE) from None
    return Response(
        content=render_report_pdf(content),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="report-{session.id}.pdf"',
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
