"""Candidate edits to a resume extraction (CT-28; CV-10, LAC-21).

Only ``ready`` versions can be edited (``RESUME_NOT_READY`` otherwise). Every item the
candidate adds or edits becomes ``origin="user_provided"`` with ``evidence=[]``: it no
longer quotes the resume text. Removing an item deletes it from the extraction.

The version row is re-read with ``SELECT ... FOR UPDATE`` (``populate_existing``) before each
change, so the status and extraction checked are the committed ones and concurrent edits
never overwrite each other. None of these functions commit (CT-2): the caller does.

Session snapshots (CV-12) are copies taken when a session starts and are never touched here.
"""

import unicodedata
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import RESOURCE_NOT_FOUND, RESUME_NOT_READY, VALIDATION_ERROR, AppError
from app.models.resume import ExtractionItem, ExtractionKind, Resume, ResumeStatus
from app.observability import log_event

ALLOWED_KINDS: frozenset[str] = frozenset({"experience", "education", "skill"})
ALLOWED_FIELD_KEYS: frozenset[str] = frozenset(
    {"title", "organization", "start", "end", "degree", "institution", "name", "description"}
)
FIELD_VALUE_MAX_LENGTH = 2000
ITEM_ID_MAX_LENGTH = 64
MAX_ITEMS_PER_RESUME = 200

# Whitespace control characters kept in values (multi-line descriptions).
_ALLOWED_CONTROL_CHARS = frozenset({"\n", "\r", "\t"})


class ItemInput(BaseModel):
    """Candidate input for one extraction item.

    ``kind`` is required when adding; when editing, ``None`` keeps the current kind.
    ``fields`` replaces the item's fields entirely. The editing functions validate both
    again, so an instance built without validation is still safe.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ExtractionKind | None = None
    fields: dict[str, str]


def _invalid(loc: str, error_type: str) -> AppError:
    # Only the location and the error type are exposed: the input is never echoed or logged.
    return AppError.from_catalog(VALIDATION_ERROR, {"fields": [{"loc": [loc], "type": error_type}]})


def _has_unsafe_char(text: str) -> bool:
    for char in text:
        category = unicodedata.category(char)
        if category == "Cs" or (category == "Cc" and char not in _ALLOWED_CONTROL_CHARS):
            return True
    return False


def _clean_fields(fields: object) -> dict[str, str]:
    if not isinstance(fields, dict):
        raise _invalid("fields", "dict_type")
    if len(fields) > len(ALLOWED_FIELD_KEYS):
        raise _invalid("fields", "too_many_keys")
    cleaned: dict[str, str] = {}
    for key, value in fields.items():
        if not isinstance(key, str) or key not in ALLOWED_FIELD_KEYS:
            raise _invalid("fields", "unknown_key")
        if not isinstance(value, str):
            raise _invalid("fields", "string_type")
        if len(value) > FIELD_VALUE_MAX_LENGTH:
            raise _invalid("fields", "string_too_long")
        if _has_unsafe_char(value):
            raise _invalid("fields", "invalid_characters")
        stripped = value.strip()
        if stripped:
            cleaned[key] = stripped
    if not cleaned:
        raise _invalid("fields", "empty")
    return cleaned


def _parse_kind(kind: object) -> ExtractionKind:
    if not isinstance(kind, str) or kind not in ALLOWED_KINDS:
        raise _invalid("kind", "enum")
    parsed: ExtractionKind = kind  # type: ignore[assignment]  # narrowed by ALLOWED_KINDS
    return parsed


def _require_input(data: object) -> ItemInput:
    if not isinstance(data, ItemInput):
        raise _invalid("body", "model_type")
    return data


def _parse_item_id(item_id: object) -> str:
    if (
        not isinstance(item_id, str)
        or not item_id
        or len(item_id) > ITEM_ID_MAX_LENGTH
        or _has_unsafe_char(item_id)
    ):
        raise AppError.from_catalog(RESOURCE_NOT_FOUND)
    return item_id


def _lock_ready(db: Session, resume: Resume) -> Resume:
    """Re-read the version under ``FOR UPDATE`` and require it to be ``ready``."""
    if not isinstance(resume, Resume):
        raise AppError.from_catalog(RESOURCE_NOT_FOUND)
    statement = (
        select(Resume)
        .where(Resume.id == resume.id, Resume.user_id == resume.user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked = db.execute(statement).scalar_one_or_none()
    if locked is None:
        raise AppError.from_catalog(RESOURCE_NOT_FOUND)
    if locked.status != ResumeStatus.READY:
        raise AppError.from_catalog(RESUME_NOT_READY)
    return locked


def _items(resume: Resume) -> list[dict[str, Any]]:
    return [dict(entry) for entry in (resume.extraction or [])]


def _index_of(items: list[dict[str, Any]], item_id: str) -> int:
    for index, entry in enumerate(items):
        if entry.get("id") == item_id:
            return index
    raise AppError.from_catalog(RESOURCE_NOT_FOUND)


def _save(db: Session, resume: Resume, items: list[dict[str, Any]]) -> None:
    # A new list object so the JSONB column is flagged as changed.
    resume.extraction = items
    db.flush()


def add_item(db: Session, resume: Resume, data: ItemInput) -> ExtractionItem:
    """Append a ``user_provided`` item (no evidence) to a ``ready`` version.

    Raises ``AppError`` ``VALIDATION_ERROR`` (invalid kind/fields or item limit reached),
    ``RESUME_NOT_READY`` or ``RESOURCE_NOT_FOUND``. The caller commits.
    """
    payload = _require_input(data)
    kind = _parse_kind(payload.kind)
    fields = _clean_fields(payload.fields)
    locked = _lock_ready(db, resume)

    items = _items(locked)
    if len(items) >= MAX_ITEMS_PER_RESUME:
        raise _invalid("items", "too_many_items")
    item = ExtractionItem(
        id=uuid.uuid4().hex, kind=kind, fields=fields, origin="user_provided", evidence=[]
    )
    items.append(item.model_dump(mode="json"))
    _save(db, locked, items)
    log_event("resume.item_added", resume_id=str(locked.id), kind=kind)
    return item


def update_item(db: Session, resume: Resume, item_id: str, data: ItemInput) -> ExtractionItem:
    """Replace an item's fields (and kind, when given); it becomes ``user_provided``.

    The edited item loses its evidence (LAC-21). Raises ``AppError`` ``VALIDATION_ERROR``,
    ``RESUME_NOT_READY`` or ``RESOURCE_NOT_FOUND`` (unknown or malformed ``item_id``).
    The caller commits.
    """
    wanted_id = _parse_item_id(item_id)
    payload = _require_input(data)
    new_kind = None if payload.kind is None else _parse_kind(payload.kind)
    fields = _clean_fields(payload.fields)
    locked = _lock_ready(db, resume)

    items = _items(locked)
    index = _index_of(items, wanted_id)
    item = ExtractionItem(
        id=wanted_id,
        kind=new_kind or _parse_kind(items[index].get("kind")),
        fields=fields,
        origin="user_provided",
        evidence=[],
    )
    items[index] = item.model_dump(mode="json")
    _save(db, locked, items)
    log_event("resume.item_updated", resume_id=str(locked.id), kind=item.kind)
    return item


def remove_item(db: Session, resume: Resume, item_id: str) -> None:
    """Delete an item from a ``ready`` version's extraction.

    Raises ``AppError`` ``RESUME_NOT_READY`` or ``RESOURCE_NOT_FOUND`` (unknown or
    malformed ``item_id``). The caller commits.
    """
    wanted_id = _parse_item_id(item_id)
    locked = _lock_ready(db, resume)

    items = _items(locked)
    index = _index_of(items, wanted_id)
    removed_kind = items.pop(index).get("kind")
    _save(db, locked, items)
    log_event(
        "resume.item_removed",
        resume_id=str(locked.id),
        kind=removed_kind if removed_kind in ALLOWED_KINDS else "unknown",
    )
