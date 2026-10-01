"""Definitive deletion of a resume version (CT-51; DATA-03, DATA-04, DATA-90, CV-96, LAC-11).

In the caller's transaction, ``delete_resume``:

1. locks the owned version (``SELECT ... FOR UPDATE``; 404 ``RESOURCE_NOT_FOUND`` for unknown
   ids and versions of other users alike);
2. locks every session that used the version, in id order, and reduces its ``snapshot`` to
   the minimal snapshot: only ``skill`` items with quoted evidence, keeping just the skill name
   and that evidence. ``snapshot_minimal`` becomes ``True`` and ``resume_name`` ``None`` (the
   file name may contain a personal name). Sessions, answers and reports are never deleted;
3. deletes the row, which holds the extracted text and the extraction. ``resume_id`` of the
   sessions becomes ``NULL`` through the foreign key (``ON DELETE SET NULL``).

The stored file is deleted only after the caller commits (an ``after_commit`` hook on the
session), so a rollback never leaves a version without its file. If the transaction ends
without a commit the hook is dropped and the file is kept.

Lock order is fixed: the version row first, then its sessions by id. The ``resume.process``
job locks the version row before writing its result and drops it when the row is gone, so a
version deleted while ``processing`` never gets an extraction (CV-96).

Like the other service modules, this one only flushes (CT-2). Logs carry ids and counts only.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import event, select
from sqlalchemy.orm import Session, SessionTransaction

from app.models.account import User
from app.models.interview import InterviewSession
from app.models.resume import ExtractionItem
from app.observability import log_event
from app.resumes import storage
from app.resumes.service import get_owned_resume

__all__ = ["delete_resume"]

MINIMAL_KIND = "skill"
MINIMAL_FIELD = "name"
_PENDING_KEY = "privacy.pending_resume_files"


@dataclass(frozen=True)
class _PendingFile:
    transaction: SessionTransaction | None
    resume_id: UUID
    key: str


def _minimal_item(raw: Any) -> dict[str, Any] | None:
    """Return the minimal form of a snapshot item, or ``None`` when it is not kept."""
    if not isinstance(raw, dict):
        return None
    if raw.get("kind") != MINIMAL_KIND:
        return None
    evidence = raw.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return None
    fields = raw.get("fields")
    name = fields.get(MINIMAL_FIELD) if isinstance(fields, dict) else None
    candidate = {
        "id": raw.get("id"),
        "kind": MINIMAL_KIND,
        "fields": {MINIMAL_FIELD: name} if name is not None else {},
        "origin": raw.get("origin"),
        "evidence": evidence,
    }
    try:
        item = ExtractionItem.model_validate(candidate)
    except ValueError:
        # A malformed stored item is dropped rather than kept with unknown content.
        return None
    return item.model_dump(mode="json")


def _minimal_snapshot(snapshot: Any) -> list[dict[str, Any]]:
    if not isinstance(snapshot, list):
        return []
    return [item for item in (_minimal_item(raw) for raw in snapshot) if item is not None]


def _lock_sessions(db: Session, user_id: UUID, resume_id: UUID) -> list[InterviewSession]:
    statement = (
        select(InterviewSession)
        .where(InterviewSession.resume_id == resume_id, InterviewSession.user_id == user_id)
        .order_by(InterviewSession.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(db.execute(statement).scalars().all())


def _delete_file(resume_id: UUID, key: str) -> None:
    try:
        storage.delete_file(key)
    except Exception as error:  # noqa: BLE001 - the deletion is already committed
        log_event(
            "resume.file_delete_failed",
            resume_id=str(resume_id),
            error_code=type(error).__name__,
        )
        return
    log_event("resume.file_deleted", resume_id=str(resume_id))


def _pending(db: Session) -> list[_PendingFile]:
    pending: list[_PendingFile] = db.info.setdefault(_PENDING_KEY, [])
    return pending


def _on_commit(db: Session) -> None:
    # Fired only when the outermost transaction commits, before it is closed.
    pending = _pending(db)
    committed = list(pending)
    pending.clear()
    for item in committed:
        _delete_file(item.resume_id, item.key)


def _on_transaction_end(db: Session, transaction: SessionTransaction) -> None:
    # A transaction that ended without committing drops its pending deletions.
    pending = _pending(db)
    pending[:] = [item for item in pending if item.transaction is not transaction]


def _delete_file_after_commit(db: Session, resume_id: UUID, key: str | None) -> None:
    """Delete the stored file once the current transaction commits; drop it otherwise."""
    if key is None:
        return
    if not event.contains(db, "after_commit", _on_commit):
        event.listen(db, "after_commit", _on_commit)
        event.listen(db, "after_transaction_end", _on_transaction_end)
    root = db.get_transaction()
    _pending(db).append(_PendingFile(transaction=root, resume_id=resume_id, key=key))


def delete_resume(db: Session, user: User, resume_id: UUID) -> None:
    """Delete a version owned by ``user`` and reduce the sessions that used it (CT-51).

    Raises ``AppError`` ``RESOURCE_NOT_FOUND`` (404) for unknown or malformed ids and for
    versions of other users; nothing is changed then. The caller commits; the file is deleted
    after that commit.
    """
    resume = get_owned_resume(db, user, resume_id, for_update=True)
    deleted_id, key = resume.id, resume.storage_key

    sessions = _lock_sessions(db, user.id, deleted_id)
    for session in sessions:
        session.snapshot = _minimal_snapshot(session.snapshot)
        session.snapshot_minimal = True
        session.resume_name = None

    db.delete(resume)
    db.flush()
    _delete_file_after_commit(db, deleted_id, key)
    log_event("resume.deleted", resume_id=str(deleted_id), sessions_reduced=len(sessions))
