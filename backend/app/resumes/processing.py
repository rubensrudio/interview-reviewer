"""Asynchronous processing of an uploaded resume version (CT-27; CV-05..07, CV-09, CV-14,
CV-92, CV-93, CV-96, KNOW-92).

``process_resume`` is the handler of the ``resume.process`` job. It moves a version from
``received`` to ``processing`` and then to ``ready`` (text and extraction stored) or ``failed``
(``failure_code`` set, no extraction stored).

Transactions:

1. The ``processing`` mark is committed right away in a short-lived session of its own, so the
   resume list shows it while the PDF is parsed and the LLM runs. No lock on the version is
   held during that work, so a deletion is never blocked by a slow inference.
2. The outcome is written in the worker's session, which the handler never commits (CT-7):
   the worker commits it together with the ``done`` status of the job. Before writing, the
   row is read again with ``SELECT ... FOR UPDATE``; when the version was deleted meanwhile
   the result is dropped (CV-96).

Versions already ``ready`` or ``failed`` are never processed again. Expected failures (PDF,
language, LLM) end as ``failed`` with a machine code; unexpected errors (e.g. database) are
raised again after the version is marked ``failed`` in a fresh session, so a version never
stays ``processing`` once its job failed. Logs carry the version id, status, failure code
and duration, never resume content.
"""

import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_sessionmaker
from app.llm.client import get_llm_client
from app.models.resume import ExtractionItem, Resume, ResumeStatus
from app.observability import log_event
from app.resumes import storage
from app.resumes.extraction import ExtractionFailed, extract_resume_items
from app.resumes.language import is_predominantly_english
from app.resumes.ocr import ocr_pdf_text
from app.resumes.pdf import PdfCorrupted, PdfEncrypted, PdfNoText, extract_pdf_text
from app.resumes.service import RESUME_PROCESS_JOB

__all__ = [
    "CORRUPTED",
    "EXTRACTION_INVALID",
    "LLM_UNAVAILABLE",
    "NOT_ENGLISH",
    "NO_TEXT",
    "OCR_NO_TEXT",
    "PROTECTED",
    "RESUME_PROCESS_JOB",
    "process_resume",
]

NO_TEXT = "NO_TEXT"
OCR_NO_TEXT = "OCR_NO_TEXT"
NOT_ENGLISH = "NOT_ENGLISH"
CORRUPTED = "CORRUPTED"
PROTECTED = "PROTECTED"
EXTRACTION_INVALID = "EXTRACTION_INVALID"
LLM_UNAVAILABLE = "LLM_UNAVAILABLE"

_FINISHED = frozenset({ResumeStatus.READY, ResumeStatus.FAILED})
_UNFINISHED = (ResumeStatus.RECEIVED, ResumeStatus.PROCESSING)

# Code used when processing stops on an unexpected error (storage, database): the user is
# told to try again later, as for an unavailable inference server (CV-93).
UNEXPECTED_ERROR_CODE = LLM_UNAVAILABLE
FAILURE_MARK_LOCK_TIMEOUT = "5s"


@dataclass(frozen=True)
class _Outcome:
    failure_code: str | None = None
    text: str | None = None
    items: list[ExtractionItem] = field(default_factory=list)


def _parse_payload(payload: dict[str, str]) -> uuid.UUID:
    raw = payload.get("resume_id") if isinstance(payload, dict) else None
    if not isinstance(raw, str):
        raise ValueError("resume.process payload requires a resume_id")
    try:
        return uuid.UUID(raw)
    except ValueError:
        # The raw value is never echoed back.
        raise ValueError("resume.process payload has an invalid resume_id") from None


def _ocr_fallback(data: bytes) -> str:
    """OCR hook for PDFs without a text layer; ``OcrNoText`` is a ``PdfNoText`` (CT-29)."""
    return ocr_pdf_text(data)


def _mark_processing(resume_id: uuid.UUID) -> None:
    """Commit ``received`` -> ``processing`` in a session of its own so readers see it."""
    with get_sessionmaker()() as session:
        changed = session.execute(
            update(Resume)
            .where(Resume.id == resume_id, Resume.status == ResumeStatus.RECEIVED)
            .values(status=ResumeStatus.PROCESSING)
        ).rowcount  # type: ignore[attr-defined]
        session.commit()
    if changed:
        log_event(
            "resume.status_changed",
            resume_id=str(resume_id),
            **{"from": ResumeStatus.RECEIVED.value, "to": ResumeStatus.PROCESSING.value},
        )


def _read_text(data: bytes) -> str | _Outcome:
    try:
        return extract_pdf_text(data)
    except PdfEncrypted:
        return _Outcome(failure_code=PROTECTED)
    except PdfCorrupted:
        return _Outcome(failure_code=CORRUPTED)
    except PdfNoText:
        if not get_settings().ocr_enabled:
            return _Outcome(failure_code=NO_TEXT)
    try:
        return _ocr_fallback(data)
    except PdfNoText:
        return _Outcome(failure_code=OCR_NO_TEXT)


def _run_pipeline(storage_key: str | None) -> _Outcome:
    """Parse, check and extract the resume. Never touches the database."""
    if storage_key is None:
        return _Outcome(failure_code=CORRUPTED)
    try:
        data = storage.read_file(storage_key)
    except (FileNotFoundError, ValueError):
        return _Outcome(failure_code=CORRUPTED)

    text = _read_text(data)
    if isinstance(text, _Outcome):
        return text
    if not is_predominantly_english(text):
        return _Outcome(failure_code=NOT_ENGLISH)

    try:
        llm = get_llm_client()
    except ValueError:
        # Misconfigured inference server (e.g. host outside llm_allowed_hosts).
        log_event("resume.llm_client_unavailable", reason="invalid_configuration")
        return _Outcome(failure_code=LLM_UNAVAILABLE)
    try:
        items = extract_resume_items(llm, text)
    except ExtractionFailed as error:
        code = LLM_UNAVAILABLE if error.reason == "llm_unavailable" else EXTRACTION_INVALID
        return _Outcome(failure_code=code)
    if not items:
        # Nothing survived the evidence filter: there is no extraction to show (CV-07, CV-09).
        return _Outcome(failure_code=EXTRACTION_INVALID)
    return _Outcome(text=text, items=items)


def _lock_resume(db: Session, resume_id: uuid.UUID) -> Resume | None:
    statement = (
        select(Resume)
        .where(Resume.id == resume_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return db.execute(statement).scalar_one_or_none()


def _duration_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _fail_after_error(resume_id: uuid.UUID, error: Exception) -> None:
    """Mark an unfinished version ``failed`` in a fresh session after an unexpected error.

    The worker session may be broken (e.g. lost connection), so a new one is used. A short
    lock timeout keeps the worker from hanging when the row is locked elsewhere.
    """
    try:
        with get_sessionmaker()() as session:
            session.execute(sql_text(f"SET LOCAL lock_timeout = '{FAILURE_MARK_LOCK_TIMEOUT}'"))
            changed = session.execute(
                update(Resume)
                .where(Resume.id == resume_id, Resume.status.in_(_UNFINISHED))
                .values(
                    status=ResumeStatus.FAILED,
                    failure_code=UNEXPECTED_ERROR_CODE,
                    extracted_text=None,
                    extraction=None,
                )
            ).rowcount  # type: ignore[attr-defined]
            session.commit()
    except Exception as mark_error:
        log_event(
            "resume.failure_mark_failed",
            resume_id=str(resume_id),
            error_code=type(mark_error).__name__,
        )
        return
    if changed:
        log_event(
            "resume.status_changed",
            resume_id=str(resume_id),
            failure_code=UNEXPECTED_ERROR_CODE,
            error_code=type(error).__name__,
            to=ResumeStatus.FAILED.value,
        )


def process_resume(db: Session, payload: dict[str, str]) -> None:
    """Job handler for ``resume.process``. Does not commit (CT-7).

    An unexpected error (storage, database) marks the version ``failed`` in a fresh session
    and is raised again, so the job is recorded as failed and the version never stays
    ``processing``.
    """
    resume_id = _parse_payload(payload)
    try:
        _process(db, resume_id)
    except Exception as error:
        # Release any row lock held by the worker session before the fresh session writes.
        db.rollback()
        _fail_after_error(resume_id, error)
        raise


def _process(db: Session, resume_id: uuid.UUID) -> None:
    started = time.perf_counter()

    current = db.execute(
        select(Resume.status, Resume.storage_key).where(Resume.id == resume_id)
    ).one_or_none()
    if current is None:
        log_event("resume.processing_discarded", resume_id=str(resume_id), reason="deleted")
        return
    status, storage_key = current
    if status in _FINISHED:
        log_event("resume.processing_skipped", resume_id=str(resume_id), status=status.value)
        return

    _mark_processing(resume_id)
    outcome = _run_pipeline(storage_key)

    resume = _lock_resume(db, resume_id)
    if resume is None:
        log_event(
            "resume.processing_discarded",
            resume_id=str(resume_id),
            reason="deleted",
            duration_ms=_duration_ms(started),
        )
        return
    if resume.status in _FINISHED:
        log_event("resume.processing_skipped", resume_id=str(resume_id), status=resume.status.value)
        return

    previous = resume.status
    if outcome.failure_code is None:
        resume.status = ResumeStatus.READY
        resume.failure_code = None
        resume.extracted_text = outcome.text
        resume.extraction = [item.model_dump(mode="json") for item in outcome.items]
    else:
        resume.status = ResumeStatus.FAILED
        resume.failure_code = outcome.failure_code
        resume.extracted_text = None
        resume.extraction = None
    db.flush()

    log_event(
        "resume.status_changed",
        resume_id=str(resume_id),
        failure_code=outcome.failure_code,
        duration_ms=_duration_ms(started),
        **{"from": previous.value, "to": resume.status.value},
    )
