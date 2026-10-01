"""Resume routes (plan section 8.1, "Resumes"; AUTH-16, CV-01..05, CV-07, CV-10, CV-11, CV-13,
CV-90, CV-91).

Thin HTTP layer over CT-25 (upload, listing, ownership lookup) and CT-28 (extraction edits).
The modules flush; these routes own the transaction and commit explicitly (CT-2). Anything
left uncommitted when an error is raised is rolled back by ``get_db``.

Every route depends on ``CurrentUser`` (session, CSRF on non-GET methods, current terms).
A version of another user, an unknown id and a malformed id all get the same 404
``RESOURCE_NOT_FOUND`` without any resource data (AUTH-16).

The upload route reads its multipart body only after the session and CSRF checks, through a
receive channel capped at ``max_pdf_bytes + MULTIPART_OVERHEAD_BYTES``; the file part itself
is read up to ``max_pdf_bytes + 1`` bytes so the service can tell "exactly the limit" from
"one byte above" (CV-91). The JSON edit routes keep the 16 KiB body cap of LAC-32.

Filenames and extracted content are never logged here.
"""

import re
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.types import Message, Receive

from app.api.auth_local import BoundedBodyRoute
from app.api.deps import CurrentUser, DbSession
from app.config import get_settings
from app.errors import FILE_TOO_LARGE, RESOURCE_NOT_FOUND, VALIDATION_ERROR, AppError
from app.models.account import User
from app.models.resume import ExtractionItem, ExtractionKind, Resume, ResumeStatus
from app.observability import log_event
from app.privacy.resume_deletion import delete_resume
from app.resumes import storage
from app.resumes.editing import ItemInput, add_item, remove_item, update_item
from app.resumes.processing import (
    CORRUPTED,
    EXTRACTION_INVALID,
    LLM_UNAVAILABLE,
    NO_TEXT,
    NOT_ENGLISH,
    OCR_NO_TEXT,
    PROTECTED,
)
from app.resumes.service import get_owned_resume, list_resumes, upload_resume

# Room for the multipart boundaries and part headers around the file itself.
MULTIPART_OVERHEAD_BYTES = 64 * 1024
UPLOAD_FIELD = "file"
MAX_UPLOAD_FILES = 1
MAX_UPLOAD_FIELDS = 8

# Spec section 9 texts for the asynchronous failures (shown with the version status).
NO_TEXT_MESSAGE = (
    "We couldn't read text from this PDF. Scanned documents are not supported yet — "
    "please upload a text-based PDF."
)
NOT_ENGLISH_MESSAGE = "Only resumes in English are supported at the moment."
PROCESSING_FAILED_MESSAGE = "We couldn't process this resume. Please try uploading it again later."

FAILURE_MESSAGES: dict[str, str] = {
    NO_TEXT: NO_TEXT_MESSAGE,
    OCR_NO_TEXT: NO_TEXT_MESSAGE,
    NOT_ENGLISH: NOT_ENGLISH_MESSAGE,
    CORRUPTED: PROCESSING_FAILED_MESSAGE,
    PROTECTED: PROCESSING_FAILED_MESSAGE,
    EXTRACTION_INVALID: PROCESSING_FAILED_MESSAGE,
    LLM_UNAVAILABLE: PROCESSING_FAILED_MESSAGE,
}

_CONTENT_LENGTH = re.compile(r"[0-9]{1,20}")


class ResumeSummary(BaseModel):
    id: UUID
    filename: str
    uploaded_at: datetime
    status: ResumeStatus
    failure_code: str | None
    failure_message: str | None


class ResumeDetail(ResumeSummary):
    items: list[ExtractionItem] | None


class AddItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ExtractionKind
    fields: dict[StrictStr, StrictStr]


class UpdateItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None keeps the current kind (CT-28).
    kind: ExtractionKind | None = None
    fields: dict[StrictStr, StrictStr]


router = APIRouter(prefix="/api/resumes", tags=["resumes"], route_class=BoundedBodyRoute)


# --- helpers ---------------------------------------------------------------------------------


def _failure_message(resume: Resume) -> str | None:
    if resume.status != ResumeStatus.FAILED:
        return None
    # Unknown or missing codes fall back to the generic "try again later" text.
    return FAILURE_MESSAGES.get(resume.failure_code or "", PROCESSING_FAILED_MESSAGE)


def _summary_fields(resume: Resume) -> dict[str, Any]:
    return {
        "id": resume.id,
        "filename": resume.filename,
        "uploaded_at": resume.uploaded_at,
        "status": resume.status,
        "failure_code": resume.failure_code,
        "failure_message": _failure_message(resume),
    }


def _summary(resume: Resume) -> ResumeSummary:
    return ResumeSummary(**_summary_fields(resume))


def _items(resume: Resume) -> list[ExtractionItem] | None:
    """Items of a ``ready`` version; ``None`` while it is not ready (CV-07)."""
    if resume.status != ResumeStatus.READY:
        return None
    items: list[ExtractionItem] = []
    for entry in resume.extraction or []:
        try:
            items.append(ExtractionItem.model_validate(entry))
        except ValidationError:
            # A malformed stored entry is skipped instead of failing the whole page.
            log_event("resume.item_invalid", resume_id=str(resume.id))
    return items


def _owned(db: Session, user: User, resume_id: str) -> Resume:
    """Resolve a path id into an owned version; malformed ids get the same 404 (AUTH-16)."""
    try:
        parsed = UUID(resume_id)
    except ValueError:
        raise AppError.from_catalog(RESOURCE_NOT_FOUND) from None
    return get_owned_resume(db, user, parsed)


def _too_large(limit: int) -> AppError:
    log_event("resume.upload_rejected", reason="body_too_large")
    return AppError.from_catalog(FILE_TOO_LARGE, {"limit_bytes": limit})


def _invalid_upload(error_type: str) -> AppError:
    return AppError.from_catalog(
        VALIDATION_ERROR, {"fields": [{"loc": ["body", UPLOAD_FIELD], "type": error_type}]}
    )


def _capped_receive(receive: Receive, cap: int, limit: int) -> Receive:
    """Receive channel that fails with ``FILE_TOO_LARGE`` once ``cap`` bytes have arrived.

    Counting the stream (and not only ``Content-Length``) keeps chunked bodies bounded.
    """
    total = 0

    async def capped() -> Message:
        nonlocal total
        message = await receive()
        if message["type"] == "http.request":
            total += len(message.get("body", b""))
            if total > cap:
                raise _too_large(limit)
        return message

    return capped


async def _read_form(request: Request, limit: int) -> FormData:
    cap = limit + MULTIPART_OVERHEAD_BYTES
    declared = request.headers.get("content-length")
    if declared is not None:
        if _CONTENT_LENGTH.fullmatch(declared) is None:
            raise _invalid_upload("content_length")
        if int(declared) > cap:
            raise _too_large(limit)
    bounded = Request(request.scope, _capped_receive(request.receive, cap, limit))
    try:
        return await bounded.form(max_files=MAX_UPLOAD_FILES, max_fields=MAX_UPLOAD_FIELDS)
    except (HTTPException, MultiPartException):
        # Malformed multipart, too many parts, etc. The parser message is not exposed.
        raise _invalid_upload("multipart") from None


async def _read_upload(request: Request, limit: int) -> tuple[str, bytes]:
    """Return the client filename and at most ``limit + 1`` bytes of the ``file`` part."""
    form = await _read_form(request, limit)
    try:
        part = form.get(UPLOAD_FIELD)
        if not isinstance(part, UploadFile):
            raise _invalid_upload("missing")
        data = await part.read(limit + 1)
        filename = part.filename if isinstance(part.filename, str) else ""
    finally:
        await form.close()
    return filename, data


def _discard_file(resume_id: UUID, key: str | None) -> None:
    if key is None:
        return
    try:
        storage.delete_file(key)
    except Exception:  # noqa: BLE001 - the commit error must be the one that propagates
        log_event("resume.orphan_cleanup_failed", resume_id=str(resume_id))


def _store_upload(db: Session, user: User, filename: str, data: bytes) -> ResumeSummary:
    resume = upload_resume(db, user, filename, data)
    resume_id, key = resume.id, resume.storage_key
    try:
        db.commit()
    except BaseException:
        # upload_resume wrote the file before this commit: do not leave it orphaned.
        _discard_file(resume_id, key)
        raise
    return _summary(resume)


# --- routes ----------------------------------------------------------------------------------


async def upload(request: Request, user: CurrentUser, db: DbSession) -> ResumeSummary:
    # The body is read only here, after CurrentUser checked the session and CSRF header.
    limit = get_settings().max_pdf_bytes
    filename, data = await _read_upload(request, limit)
    # Database and filesystem work is synchronous: keep it off the event loop.
    return await run_in_threadpool(_store_upload, db, user, filename, data)


# Registered with the plain APIRoute: BoundedBodyRoute would cap the PDF at 16 KiB.
router.add_api_route(
    "",
    upload,
    methods=["POST"],
    status_code=201,
    response_model=ResumeSummary,
    route_class_override=APIRoute,
)


@router.get("")
def list_own(
    user: CurrentUser,
    db: DbSession,
    status: Annotated[ResumeStatus | None, Query()] = None,
) -> list[ResumeSummary]:
    return [_summary(resume) for resume in list_resumes(db, user, status)]


@router.get("/{resume_id}")
def get_one(resume_id: str, user: CurrentUser, db: DbSession) -> ResumeDetail:
    resume = _owned(db, user, resume_id)
    return ResumeDetail(**_summary_fields(resume), items=_items(resume))


@router.post("/{resume_id}/items", status_code=201)
def add(resume_id: str, body: AddItemRequest, user: CurrentUser, db: DbSession) -> ExtractionItem:
    resume = _owned(db, user, resume_id)
    item = add_item(db, resume, ItemInput(kind=body.kind, fields=body.fields))
    db.commit()
    return item


@router.patch("/{resume_id}/items/{item_id}")
def update(
    resume_id: str, item_id: str, body: UpdateItemRequest, user: CurrentUser, db: DbSession
) -> ExtractionItem:
    resume = _owned(db, user, resume_id)
    item = update_item(db, resume, item_id, ItemInput(kind=body.kind, fields=body.fields))
    db.commit()
    return item


@router.delete("/{resume_id}", status_code=204, response_class=Response)
def delete_one(resume_id: str, user: CurrentUser, db: DbSession) -> None:
    # Malformed ids get the same 404 as unknown ones (AUTH-16); the file goes after commit.
    try:
        parsed = UUID(resume_id)
    except ValueError:
        raise AppError.from_catalog(RESOURCE_NOT_FOUND) from None
    delete_resume(db, user, parsed)
    db.commit()


@router.delete("/{resume_id}/items/{item_id}", status_code=204, response_class=Response)
def remove(resume_id: str, item_id: str, user: CurrentUser, db: DbSession) -> None:
    resume = _owned(db, user, resume_id)
    remove_item(db, resume, item_id)
    db.commit()


__all__ = ["router"]
