"""Resume upload, listing and ownership lookup (CT-25; CV-01..04, CV-11, CV-13, CV-90..95).

Upload checks run in a fixed order: size (``FILE_TOO_LARGE``), content (``INVALID_PDF``) and,
under a ``SELECT ... FOR UPDATE`` on the owner's row, the number of stored versions
(``RESUME_LIMIT_REACHED``). Only then is the file written, the ``received`` version created
and the ``resume.process`` job enqueued. Nothing is written when a check fails.

None of these functions commit (CT-2): the caller commits the version and its job together.
The PDF is never parsed here; the ``resume.process`` job does it asynchronously.
"""

import unicodedata
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import (
    FILE_TOO_LARGE,
    INVALID_PDF,
    RESOURCE_NOT_FOUND,
    RESUME_LIMIT_REACHED,
    VALIDATION_ERROR,
    AppError,
)
from app.jobs.queue import enqueue
from app.models.account import User
from app.models.resume import Resume, ResumeStatus
from app.observability import log_event
from app.resumes import storage
from app.resumes.pdf import looks_like_pdf

RESUME_PROCESS_JOB = "resume.process"
FILENAME_MAX_LENGTH = 255
DEFAULT_FILENAME = "resume.pdf"


def _rejected(code: str, reason: str, details: dict[str, object] | None = None) -> AppError:
    log_event("resume.upload_rejected", reason=reason)
    return AppError.from_catalog(code, details)


def _is_allowed_filename_char(char: str) -> bool:
    # Drops NUL and other control characters (Cc), format characters (Cf), lone
    # surrogates (Cs) and unassigned code points (Cn); all of them are unsafe to store/echo.
    return unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Cn"}


def _sanitize_filename(filename: object) -> str:
    """Return a display-safe base name; the client-provided path is never kept."""
    if not isinstance(filename, str):
        raise AppError.from_catalog(
            VALIDATION_ERROR, {"fields": [{"loc": ["filename"], "type": "string_type"}]}
        )
    cleaned = "".join(char for char in filename if _is_allowed_filename_char(char))
    base_name = cleaned.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if base_name in {"", ".", ".."}:
        return DEFAULT_FILENAME
    return base_name[:FILENAME_MAX_LENGTH]


def _lock_owner(db: Session, user_id: UUID) -> None:
    """Serialize concurrent uploads of the same account on its ``users`` row (CV-95)."""
    locked = db.execute(
        select(User.id).where(User.id == user_id).with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise AppError.from_catalog(RESOURCE_NOT_FOUND)


def _count_versions(db: Session, user_id: UUID) -> int:
    return db.execute(
        select(func.count()).select_from(Resume).where(Resume.user_id == user_id)
    ).scalar_one()


def upload_resume(db: Session, user: User, filename: str, data: bytes) -> Resume:
    """Store a new resume version in ``received`` state and enqueue its processing.

    Raises ``AppError`` with ``VALIDATION_ERROR`` (filename not a string),
    ``FILE_TOO_LARGE`` (``details.limit_bytes``), ``INVALID_PDF`` (empty or non-PDF
    content, whatever the extension) or ``RESUME_LIMIT_REACHED`` (the account already has
    ``max_resumes_per_user`` versions in any state). The caller commits.
    """
    settings = get_settings()
    safe_filename = _sanitize_filename(filename)

    if not isinstance(data, bytes | bytearray):
        raise _rejected(INVALID_PDF, "not_bytes")
    if len(data) > settings.max_pdf_bytes:
        raise _rejected(FILE_TOO_LARGE, "too_large", {"limit_bytes": settings.max_pdf_bytes})
    if not looks_like_pdf(bytes(data)):
        raise _rejected(INVALID_PDF, "not_pdf")

    _lock_owner(db, user.id)
    if _count_versions(db, user.id) >= settings.max_resumes_per_user:
        raise _rejected(RESUME_LIMIT_REACHED, "limit_reached")

    resume_id = uuid4()
    storage_key = storage.save_file(user.id, resume_id, bytes(data))
    try:
        resume = Resume(
            id=resume_id,
            user_id=user.id,
            filename=safe_filename,
            status=ResumeStatus.RECEIVED,
            storage_key=storage_key,
        )
        db.add(resume)
        db.flush()
        enqueue(db, RESUME_PROCESS_JOB, {"resume_id": str(resume_id)})
    except BaseException:
        # Keep "nothing is stored" true when the database write fails after the file write.
        storage.delete_file(storage_key)
        raise

    db.refresh(resume)
    log_event("resume.uploaded", resume_id=str(resume_id), size_bytes=len(data))
    return resume


def _parse_status(status: object) -> ResumeStatus | None:
    if status is None or isinstance(status, ResumeStatus):
        return status
    if isinstance(status, str):
        allowed = {member.value: member for member in ResumeStatus}
        if status in allowed:
            return allowed[status]
    raise AppError.from_catalog(VALIDATION_ERROR, {"fields": [{"loc": ["status"], "type": "enum"}]})


def list_resumes(db: Session, user: User, status: ResumeStatus | None = None) -> list[Resume]:
    """Return the user's own versions, newest first, optionally filtered by ``status``.

    Raises ``AppError`` with ``VALIDATION_ERROR`` for a status outside ``ResumeStatus``.
    """
    wanted = _parse_status(status)
    statement = select(Resume).where(Resume.user_id == user.id)
    if wanted is not None:
        statement = statement.where(Resume.status == wanted)
    statement = statement.order_by(Resume.uploaded_at.desc(), Resume.id)
    return list(db.execute(statement).scalars().all())


def _parse_resume_id(resume_id: object) -> UUID | None:
    if isinstance(resume_id, UUID):
        return resume_id
    if isinstance(resume_id, str):
        try:
            return UUID(resume_id)
        except ValueError:
            return None
    return None


def get_owned_resume(db: Session, user: User, resume_id: UUID, for_update: bool = False) -> Resume:
    """Return a version owned by ``user``, optionally locked with ``SELECT ... FOR UPDATE``.

    Raises ``AppError`` ``RESOURCE_NOT_FOUND`` (404) for unknown ids, malformed ids and
    versions of other users alike, so ownership is never disclosed.
    """
    parsed_id = _parse_resume_id(resume_id)
    if parsed_id is None:
        raise AppError.from_catalog(RESOURCE_NOT_FOUND)
    statement = select(Resume).where(Resume.id == parsed_id, Resume.user_id == user.id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    resume = db.execute(statement).scalar_one_or_none()
    if resume is None:
        raise AppError.from_catalog(RESOURCE_NOT_FOUND)
    return resume
