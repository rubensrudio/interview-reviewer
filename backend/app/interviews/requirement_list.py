"""Requirement list editing, list confirmation and plan confirmation (CT-38).

The candidate edits the structured requirement list while the session is
``awaiting_confirmation`` (PLAN-05, PLAN-11), confirms it to get a plan proposal with one
question per distinct required skill (PLAN-09, N = M) and then confirms the plan, which fixes
``planned_count`` and the plan skills, moves the session to ``preparing_questions`` and queues
question preparation (PLAN-10).

Skills are never grouped nor dropped automatically (PLAN-08, LAC-05): merging and unmerging
only happen through ``merge_items``/``split_item``, which build the list the candidate then
submits with ``replace_requirement_list``.

``pending_clarification`` is server-controlled (PLAN-04): an item keeps it while its id, name
and classification are unchanged; editing the name or the classification of a pending item
resolves it. New, merged and split items are never pending.

None of these functions commit (CT-2). Callers pass a session locked with
``get_owned_session(..., for_update=True)`` and commit afterwards.
"""

import re
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import (
    INVALID_STATE,
    NO_REQUIRED_SKILLS,
    PENDING_CLARIFICATION,
    TOO_MANY_REQUIRED_SKILLS,
    VALIDATION_ERROR,
    AppError,
)
from app.interviews.sessions import touch_activity
from app.interviews.state_machine import transition
from app.jobs.queue import enqueue
from app.logging_setup import log_event
from app.models.interview import (
    ExpectedLevel,
    InterviewSession,
    RequirementItem,
    SessionStatus,
)

__all__ = [
    "MAX_ITEMS",
    "PREPARE_QUESTIONS_JOB",
    "ConfirmationError",
    "PlanProposal",
    "RequirementItemInput",
    "confirm_plan",
    "confirm_requirement_list",
    "confirmation_errors",
    "merge_items",
    "replace_requirement_list",
    "split_item",
]

PREPARE_QUESTIONS_JOB = "session.prepare_questions"

# Defensive bounds on candidate input; well above the 20 required skills limit (PLAN-08).
MAX_ITEMS = 100
MAX_TERMS_PER_ITEM = 50
NAME_MAX_LENGTH = 255
ID_MAX_LENGTH = 64

# NUL cannot be stored in PostgreSQL text/JSONB and is dropped; other control characters
# (C0, DEL, C1) and lone surrogates (not encodable as UTF-8) become spaces, as in the other
# interview inputs. Names are single-line, so whitespace runs collapse to one space.
_NUL_RE = re.compile("\x00")
_CONTROL_RE = re.compile("[\x01-\x1f\x7f-\x9f\ud800-\udfff]")


def _clean_text(value: object) -> object:
    """Sanitize a candidate-provided name; non-strings are left for pydantic to reject."""
    if not isinstance(value, str):
        return value
    return " ".join(_CONTROL_RE.sub(" ", _NUL_RE.sub("", value)).split())


_Text = Annotated[
    str,
    BeforeValidator(_clean_text),
    StringConstraints(min_length=1, max_length=NAME_MAX_LENGTH),
]


class RequirementItemInput(BaseModel):
    """One requirement as submitted by the candidate when editing the list (PLAN-05).

    ``id`` is the id of an existing item, or ``None`` for a new one. Server-controlled fields
    of ``RequirementItem`` (``pending_clarification``, ``clarification_question``) are
    ignored, so the client may send back the items it received.
    """

    model_config = ConfigDict(extra="ignore")

    id: (
        Annotated[
            str,
            BeforeValidator(_clean_text),
            StringConstraints(min_length=1, max_length=ID_MAX_LENGTH),
        ]
        | None
    ) = None
    name: _Text
    original_terms: list[_Text] = Field(default_factory=list, max_length=MAX_TERMS_PER_ITEM)
    classification: Literal["required", "nice_to_have"]
    level: ExpectedLevel | None = None

    @field_validator("original_terms", mode="before")
    @classmethod
    def _drop_blank_terms(cls, terms: object) -> object:
        # A term left empty after sanitizing is dropped instead of rejecting the item.
        if not isinstance(terms, list):
            return terms
        cleaned = [_clean_text(term) for term in terms]
        return [term for term in cleaned if term != ""]

    @field_validator("original_terms")
    @classmethod
    def _dedupe_terms(cls, terms: list[str]) -> list[str]:
        return _dedupe(terms)


@dataclass(frozen=True)
class ConfirmationError:
    """A reason the list cannot be confirmed; ``code`` is a section 8.3 catalog code."""

    code: str
    details: dict[str, Any] | None = field(default=None)


class PlanProposal(BaseModel):
    """Plan shown for confirmation: N = M questions, one per required skill (PLAN-09)."""

    model_config = ConfigDict(frozen=True)

    planned_count: int
    skills: list[str]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _skill_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def _new_id() -> str:
    return uuid.uuid4().hex


def _validation_error(loc: list[str | int], error_type: str) -> AppError:
    return AppError.from_catalog(VALIDATION_ERROR, {"fields": [{"loc": loc, "type": error_type}]})


def _require_status(session: InterviewSession, status: SessionStatus) -> None:
    if session.status != status:
        raise AppError.from_catalog(INVALID_STATE)


def _load_items(session: InterviewSession) -> list[RequirementItem]:
    return [RequirementItem.model_validate(item) for item in session.requirement_items or []]


def _store_items(session: InterviewSession, items: list[RequirementItem]) -> None:
    # A new list object so SQLAlchemy detects the JSONB change.
    session.requirement_items = [item.model_dump(mode="json") for item in items]


def _required_skills(items: list[RequirementItem]) -> list[str]:
    """Distinct required skill names, in list order (first spelling wins)."""
    seen: set[str] = set()
    skills: list[str] = []
    for item in items:
        if item.classification != "required":
            continue
        key = _skill_key(item.name)
        if key not in seen:
            seen.add(key)
            skills.append(item.name)
    return skills


def _keeps_pending(previous: RequirementItem | None, submitted: RequirementItemInput) -> bool:
    return (
        previous is not None
        and previous.pending_clarification
        and _skill_key(previous.name) == _skill_key(submitted.name)
        and previous.classification == submitted.classification
    )


def replace_requirement_list(
    db: Session, session: InterviewSession, items: list[RequirementItemInput]
) -> None:
    """Replace the whole requirement list with the candidate's edited version (PLAN-05).

    Only allowed in ``awaiting_confirmation`` (``INVALID_STATE`` otherwise, e.g. after the
    plan was confirmed, PLAN-10). Duplicate ids or more than ``MAX_ITEMS`` items raise
    ``VALIDATION_ERROR`` without changing the session. Any previous proposal is discarded,
    since it no longer matches the list.
    """
    _require_status(session, SessionStatus.AWAITING_CONFIRMATION)
    if len(items) > MAX_ITEMS:
        raise _validation_error(["items"], "too_long")

    seen_ids: set[str] = set()
    for index, submitted in enumerate(items):
        if submitted.id is None:
            continue
        if submitted.id in seen_ids:
            raise _validation_error(["items", index, "id"], "duplicate")
        seen_ids.add(submitted.id)

    previous_by_id = {item.id: item for item in _load_items(session)}
    replaced: list[RequirementItem] = []
    for submitted in items:
        previous = previous_by_id.get(submitted.id) if submitted.id is not None else None
        pending = _keeps_pending(previous, submitted)
        replaced.append(
            RequirementItem(
                id=submitted.id if submitted.id is not None else _new_id(),
                name=submitted.name,
                original_terms=submitted.original_terms or [submitted.name],
                classification=submitted.classification,
                level=submitted.level,
                pending_clarification=pending,
                clarification_question=(
                    previous.clarification_question if pending and previous else None
                ),
            )
        )

    _store_items(session, replaced)
    session.proposal = None
    touch_activity(session)
    db.flush()
    log_event("session.requirements_edited", session_id=str(session.id), items=len(replaced))


def confirmation_errors(items: list[RequirementItem], max_required: int) -> list[ConfirmationError]:
    """Return why ``items`` cannot be confirmed; empty when the list is valid.

    Order: ``NO_REQUIRED_SKILLS`` (PLAN-07), ``PENDING_CLARIFICATION`` (PLAN-04),
    ``TOO_MANY_REQUIRED_SKILLS`` with ``count`` and ``excess`` over distinct required skills
    (PLAN-08, PLAN-91).
    """
    errors: list[ConfirmationError] = []
    required_count = len(_required_skills(items))
    if required_count == 0:
        errors.append(ConfirmationError(NO_REQUIRED_SKILLS))
    if any(item.pending_clarification for item in items):
        errors.append(ConfirmationError(PENDING_CLARIFICATION))
    if required_count > max_required:
        errors.append(
            ConfirmationError(
                TOO_MANY_REQUIRED_SKILLS,
                {"count": required_count, "excess": required_count - max_required},
            )
        )
    return errors


def _raise_first(errors: list[ConfirmationError]) -> None:
    if not errors:
        return
    first = errors[0]
    message_args = first.details or {}
    raise AppError.from_catalog(first.code, first.details, **message_args)


def confirm_requirement_list(db: Session, session: InterviewSession) -> PlanProposal:
    """Validate the list and store the plan proposal with N = M (PLAN-09).

    Raises ``INVALID_STATE`` outside ``awaiting_confirmation`` and the first
    ``confirmation_errors`` entry as ``AppError`` (422). The session stays in
    ``awaiting_confirmation`` until ``confirm_plan``.
    """
    _require_status(session, SessionStatus.AWAITING_CONFIRMATION)
    items = _load_items(session)
    _raise_first(confirmation_errors(items, get_settings().max_required_skills))

    skills = _required_skills(items)
    proposal = PlanProposal(planned_count=len(skills), skills=skills)
    session.proposal = proposal.model_dump(mode="json")
    touch_activity(session)
    db.flush()
    log_event(
        "session.list_confirmed", session_id=str(session.id), planned_count=proposal.planned_count
    )
    return proposal


def confirm_plan(db: Session, session: InterviewSession) -> None:
    """Fix the plan and queue question preparation (PLAN-10).

    Requires ``awaiting_confirmation`` with a stored proposal whose skills still match the
    current list (``INVALID_STATE`` otherwise, so a stale proposal is never fixed).
    Applies ``interview_level`` to items without level (LANG-02), fixes ``planned_count``,
    moves to ``preparing_questions`` and enqueues ``session.prepare_questions``.
    """
    _require_status(session, SessionStatus.AWAITING_CONFIRMATION)
    if not session.proposal:
        raise AppError.from_catalog(INVALID_STATE)
    items = _load_items(session)
    _raise_first(confirmation_errors(items, get_settings().max_required_skills))
    proposal = PlanProposal.model_validate(session.proposal)
    skills = _required_skills(items)
    if skills != proposal.skills or len(skills) != proposal.planned_count:
        raise AppError.from_catalog(INVALID_STATE)

    if session.interview_level is not None:
        level = session.interview_level
        items = [
            item if item.level is not None else item.model_copy(update={"level": level})
            for item in items
        ]
        _store_items(session, items)

    transition(session, SessionStatus.PREPARING_QUESTIONS)
    session.planned_count = proposal.planned_count
    touch_activity(session)
    db.flush()
    enqueue(db, PREPARE_QUESTIONS_JOB, {"session_id": str(session.id)})
    log_event(
        "session.plan_confirmed", session_id=str(session.id), planned_count=proposal.planned_count
    )


def merge_items(items: list[RequirementItem], ids: list[str], name: str) -> list[RequirementItem]:
    """Merge synonym items into one that shows every original term (PLAN-11).

    The merged item takes the place of the first selected item; it is required when any
    selected item is required and takes the first level found. Needs at least two distinct
    existing ids (``VALIDATION_ERROR`` otherwise). Returns a new list.
    """
    selected_ids = _dedupe(ids)
    by_id = {item.id: item for item in items}
    if len(selected_ids) < 2 or any(item_id not in by_id for item_id in selected_ids):
        raise _validation_error(["ids"], "invalid")
    merged_name = _clean_text(name)
    if not isinstance(merged_name, str) or not merged_name or len(merged_name) > NAME_MAX_LENGTH:
        raise _validation_error(["name"], "invalid")

    selected_set = set(selected_ids)
    selected = [item for item in items if item.id in selected_set]
    terms = _dedupe([term for item in selected for term in item.original_terms])
    merged = RequirementItem(
        id=_new_id(),
        name=merged_name,
        original_terms=terms or [merged_name],
        classification=(
            "required"
            if any(item.classification == "required" for item in selected)
            else "nice_to_have"
        ),
        level=next((item.level for item in selected if item.level is not None), None),
    )

    result: list[RequirementItem] = []
    for item in items:
        if item.id == selected[0].id:
            result.append(merged)
        elif item.id not in selected_set:
            result.append(item)
    return result


def split_item(items: list[RequirementItem], item_id: str) -> list[RequirementItem]:
    """Undo a merge: one item per original term, in place of the merged one (PLAN-11).

    Each new item keeps the classification and level of the merged item. The item must exist
    and have at least two original terms (``VALIDATION_ERROR`` otherwise). Returns a new list.
    """
    target = next((item for item in items if item.id == item_id), None)
    if target is None or len(_dedupe(target.original_terms)) < 2:
        raise _validation_error(["item_id"], "invalid")

    parts = [
        RequirementItem(
            id=_new_id(),
            name=term,
            original_terms=[term],
            classification=target.classification,
            level=target.level,
        )
        for term in _dedupe(target.original_terms)
    ]
    result: list[RequirementItem] = []
    for item in items:
        result.extend(parts if item.id == item_id else [item])
    return result
