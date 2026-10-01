"""Report route (plan section 8.1, "Reports"; DATA-02, EVAL-08, EVAL-12, INTV-14, AUTH-16).

Thin read-only HTTP layer over CT-34 (``Report``) and CT-36 (``get_owned_session``). The route
returns the frozen ``reports.content`` exactly as it was stored when the session completed: it
never rebuilds, recalculates or rewrites the report (EVAL-12, DATA-02).

A session of another user, an unknown id and a malformed id all get the same 404
``RESOURCE_NOT_FOUND`` without any resource data (AUTH-16). A session that is not ``completed``
gets 409 ``REPORT_NOT_AVAILABLE`` and no report data at all, so no reference point or expected
answer leaks before completion (INTV-14).
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.errors import REPORT_NOT_AVAILABLE, RESOURCE_NOT_FOUND, AppError
from app.interviews.sessions import get_owned_session
from app.models.assessment import Report
from app.models.interview import SessionStatus
from app.reports.builder import ReportContent
from app.reports.compare import compare_reports
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


def _owned_completed_report(db: DbSession, user: CurrentUser, raw_id: str) -> Report:
    """Stored report of a completed session of `user` (404 uniform, 409 when not available)."""
    try:
        parsed = UUID(raw_id)
    except ValueError:
        # Malformed ids get the same 404 as unknown ones and sessions of other users (AUTH-16).
        raise AppError.from_catalog(RESOURCE_NOT_FOUND) from None
    session = get_owned_session(db, user, parsed)
    if session.status != SessionStatus.COMPLETED:
        raise AppError.from_catalog(REPORT_NOT_AVAILABLE)
    report = db.scalar(select(Report).where(Report.session_id == session.id))
    if report is None:
        raise AppError.from_catalog(REPORT_NOT_AVAILABLE)
    return report


@router.get("/reports/compare", response_model=None)
def compare_sessions(
    a: Annotated[str, Query()], b: Annotated[str, Query()], user: CurrentUser, db: DbSession
) -> JSONResponse:
    """Compare two completed sessions of the user side by side (CMP-01, CMP-02)."""
    report_a = _owned_completed_report(db, user, a)
    report_b = _owned_completed_report(db, user, b)
    # Stored reports are only read; nothing is recalculated or written (EVAL-12).
    comparison = compare_reports(report_a, report_b)
    return JSONResponse(
        content={
            "a": {
                "session_id": str(report_a.session_id),
                "percentage": comparison.a_percentage,
                "rubric_version": report_a.rubric_version,
                "model_version": report_a.model_version,
            },
            "b": {
                "session_id": str(report_b.session_id),
                "percentage": comparison.b_percentage,
                "rubric_version": report_b.rubric_version,
                "model_version": report_b.model_version,
            },
            "common_skills": [
                {"skill": pair.skill, "a_average": pair.a_average, "b_average": pair.b_average}
                for pair in comparison.common_skills
            ],
            "comparable": comparison.comparable,
            "differences": list(comparison.differences),
        }
    )


__all__ = ["router"]
