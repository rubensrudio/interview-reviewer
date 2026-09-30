"""Integration tests for resume extraction editing (CT-28; CV-10, LAC-21)."""

import uuid
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.errors import RESOURCE_NOT_FOUND, RESUME_NOT_READY, VALIDATION_ERROR, AppError
from app.models.account import User
from app.models.resume import ExtractionItem, Resume, ResumeStatus
from app.resumes.editing import (
    FIELD_VALUE_MAX_LENGTH,
    MAX_ITEMS_PER_RESUME,
    ItemInput,
    add_item,
    remove_item,
    update_item,
)


def _user(db: Session) -> User:
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    return user


def _explicit_item(item_id: str = "exp-1") -> dict[str, Any]:
    return ExtractionItem(
        id=item_id,
        kind="experience",
        fields={"title": "Backend Engineer", "organization": "Acme"},
        origin="explicit",
        evidence=["Backend Engineer at Acme"],
    ).model_dump(mode="json")


def _skill_item(item_id: str = "skill-1") -> dict[str, Any]:
    return ExtractionItem(
        id=item_id,
        kind="skill",
        fields={"name": "Python"},
        origin="inferred",
        evidence=["Python"],
    ).model_dump(mode="json")


def _resume(
    db: Session,
    status: ResumeStatus = ResumeStatus.READY,
    extraction: list[dict[str, Any]] | None = None,
) -> Resume:
    resume = Resume(
        user_id=_user(db).id,
        filename="cv.pdf",
        status=status,
        extraction=extraction if extraction is not None else [_explicit_item(), _skill_item()],
    )
    db.add(resume)
    db.flush()
    return resume


def _stored(db: Session, resume: Resume) -> list[dict[str, Any]]:
    db.expire_all()
    refreshed = db.get(Resume, resume.id)
    assert refreshed is not None
    return refreshed.extraction or []


def _assert_validation_error(error: pytest.ExceptionInfo[AppError]) -> None:
    assert error.value.code == VALIDATION_ERROR
    assert error.value.status == 422


# --- update_item ---------------------------------------------------------------------------


def test_cv_10_editing_explicit_item_becomes_user_provided_without_evidence(
    db: Session,
) -> None:
    resume = _resume(db)

    item = update_item(
        db, resume, "exp-1", ItemInput(fields={"title": "Staff Engineer", "organization": "Acme"})
    )

    assert item.id == "exp-1"
    assert item.kind == "experience"
    assert item.origin == "user_provided"
    assert item.evidence == []
    assert item.fields == {"title": "Staff Engineer", "organization": "Acme"}
    stored = _stored(db, resume)
    assert stored[0] == item.model_dump(mode="json")
    assert stored[1] == _skill_item()


def test_cv_10_editing_keeps_the_other_items_and_their_order(db: Session) -> None:
    resume = _resume(db, extraction=[_skill_item("a"), _explicit_item("b"), _skill_item("c")])

    update_item(db, resume, "b", ItemInput(fields={"title": "CTO"}))

    assert [entry["id"] for entry in _stored(db, resume)] == ["a", "b", "c"]
    assert _stored(db, resume)[0] == _skill_item("a")


def test_cv_10_editing_can_change_the_kind(db: Session) -> None:
    resume = _resume(db)

    item = update_item(db, resume, "exp-1", ItemInput(kind="education", fields={"degree": "BSc"}))

    assert item.kind == "education"
    assert _stored(db, resume)[0]["kind"] == "education"


def test_cv_10_editing_unknown_item_is_not_found(db: Session) -> None:
    resume = _resume(db)

    with pytest.raises(AppError) as error:
        update_item(db, resume, "missing", ItemInput(fields={"name": "Go"}))

    assert error.value.code == RESOURCE_NOT_FOUND
    assert _stored(db, resume) == [_explicit_item(), _skill_item()]


def test_cv_10_editing_failed_version_is_not_ready(db: Session) -> None:
    resume = _resume(db, status=ResumeStatus.FAILED)

    with pytest.raises(AppError) as error:
        update_item(db, resume, "exp-1", ItemInput(fields={"title": "CTO"}))

    assert error.value.code == RESUME_NOT_READY
    assert error.value.status == 409
    assert _stored(db, resume) == [_explicit_item(), _skill_item()]


@pytest.mark.parametrize(
    "status", [ResumeStatus.RECEIVED, ResumeStatus.PROCESSING, ResumeStatus.FAILED]
)
def test_cv_10_every_editing_operation_requires_a_ready_version(
    db: Session, status: ResumeStatus
) -> None:
    resume = _resume(db, status=status)

    for call in (
        lambda: add_item(db, resume, ItemInput(kind="skill", fields={"name": "Go"})),
        lambda: update_item(db, resume, "exp-1", ItemInput(fields={"title": "CTO"})),
        lambda: remove_item(db, resume, "exp-1"),
    ):
        with pytest.raises(AppError) as error:
            call()
        assert error.value.code == RESUME_NOT_READY


def test_cv_10_status_is_read_under_lock_not_from_a_stale_object(db: Session) -> None:
    resume = _resume(db, status=ResumeStatus.READY)
    db.execute(
        Resume.__table__.update()
        .where(Resume.__table__.c.id == resume.id)
        .values(status=ResumeStatus.FAILED.value, extraction=None)
    )

    with pytest.raises(AppError) as error:
        update_item(db, resume, "exp-1", ItemInput(fields={"title": "CTO"}))

    assert error.value.code == RESUME_NOT_READY


# --- add_item ------------------------------------------------------------------------------


def test_cv_10_adding_a_skill_creates_user_provided_item(db: Session) -> None:
    resume = _resume(db)

    item = add_item(db, resume, ItemInput(kind="skill", fields={"name": "  Kubernetes "}))

    assert item.kind == "skill"
    assert item.origin == "user_provided"
    assert item.evidence == []
    assert item.fields == {"name": "Kubernetes"}
    assert item.id and item.id not in {"exp-1", "skill-1"}
    stored = _stored(db, resume)
    assert len(stored) == 3
    assert stored[-1] == item.model_dump(mode="json")


def test_cv_10_adding_to_a_ready_version_without_extraction_starts_the_list(
    db: Session,
) -> None:
    resume = _resume(db, extraction=[])

    item = add_item(db, resume, ItemInput(kind="education", fields={"degree": "BSc"}))

    assert _stored(db, resume) == [item.model_dump(mode="json")]


def test_cv_10_adding_without_kind_is_validation_error(db: Session) -> None:
    resume = _resume(db)

    with pytest.raises(AppError) as error:
        add_item(db, resume, ItemInput(fields={"name": "Go"}))

    _assert_validation_error(error)


def test_cv_10_user_provided_item_never_keeps_evidence(db: Session) -> None:
    resume = _resume(db)

    added = add_item(db, resume, ItemInput(kind="skill", fields={"name": "Go"}))
    edited = update_item(db, resume, "skill-1", ItemInput(fields={"name": "Python 3"}))

    assert added.evidence == [] and edited.evidence == []
    assert all(
        entry["evidence"] == []
        for entry in _stored(db, resume)
        if entry["origin"] == "user_provided"
    )


def test_cv_10_adding_beyond_the_item_limit_is_validation_error(db: Session) -> None:
    resume = _resume(db, extraction=[_skill_item(f"s{i}") for i in range(MAX_ITEMS_PER_RESUME)])

    with pytest.raises(AppError) as error:
        add_item(db, resume, ItemInput(kind="skill", fields={"name": "Go"}))

    _assert_validation_error(error)
    assert len(_stored(db, resume)) == MAX_ITEMS_PER_RESUME


# --- remove_item ---------------------------------------------------------------------------


def test_cv_10_removing_deletes_the_item(db: Session) -> None:
    resume = _resume(db)

    remove_item(db, resume, "exp-1")

    assert _stored(db, resume) == [_skill_item()]


def test_cv_10_removing_unknown_item_is_not_found(db: Session) -> None:
    resume = _resume(db)

    with pytest.raises(AppError) as error:
        remove_item(db, resume, "missing")

    assert error.value.code == RESOURCE_NOT_FOUND
    assert _stored(db, resume) == [_explicit_item(), _skill_item()]


# --- input validation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"name": "   "},
        {"unknown_key": "x"},
        {"name": 42},
        {"name": "a" * (FIELD_VALUE_MAX_LENGTH + 1)},
        {"name": "bad\x00value"},
        {"name": "bad\ud800value"},
        {"name\x00": "x"},
        {"\ud800": "x"},
    ],
)
def test_cv_10_invalid_fields_are_validation_error(db: Session, fields: dict[Any, Any]) -> None:
    resume = _resume(db)
    data = ItemInput.model_construct(kind="skill", fields=fields)

    with pytest.raises(AppError) as add_error:
        add_item(db, resume, data)
    with pytest.raises(AppError) as update_error:
        update_item(db, resume, "skill-1", data)

    _assert_validation_error(add_error)
    _assert_validation_error(update_error)
    assert _stored(db, resume) == [_explicit_item(), _skill_item()]


def test_cv_10_value_of_exactly_the_limit_is_accepted(db: Session) -> None:
    resume = _resume(db)

    item = add_item(
        db, resume, ItemInput(kind="skill", fields={"description": "a" * FIELD_VALUE_MAX_LENGTH})
    )

    assert len(item.fields["description"]) == FIELD_VALUE_MAX_LENGTH


def test_cv_10_multiline_description_is_accepted(db: Session) -> None:
    resume = _resume(db)

    item = add_item(
        db,
        resume,
        ItemInput(kind="experience", fields={"title": "Dev", "description": "a\n\tb"}),
    )

    assert item.fields["description"] == "a\n\tb"


@pytest.mark.parametrize("kind", ["project", 7, "", "SKILL"])
def test_cv_10_invalid_kind_is_validation_error(db: Session, kind: object) -> None:
    resume = _resume(db)
    data = ItemInput.model_construct(kind=kind, fields={"name": "Go"})

    with pytest.raises(AppError) as error:
        add_item(db, resume, data)

    _assert_validation_error(error)


@pytest.mark.parametrize("data", [None, {"kind": "skill", "fields": {"name": "Go"}}, "skill"])
def test_cv_10_data_that_is_not_item_input_is_validation_error(db: Session, data: object) -> None:
    resume = _resume(db)

    with pytest.raises(AppError) as error:
        add_item(db, resume, data)  # type: ignore[arg-type]

    _assert_validation_error(error)


@pytest.mark.parametrize("item_id", [None, 7, "", "a" * 65, "bad\x00id", "bad\ud800id"])
def test_cv_10_malformed_item_id_is_not_found(db: Session, item_id: object) -> None:
    resume = _resume(db)

    with pytest.raises(AppError) as update_error:
        update_item(db, resume, item_id, ItemInput(fields={"name": "Go"}))  # type: ignore[arg-type]
    with pytest.raises(AppError) as remove_error:
        remove_item(db, resume, item_id)  # type: ignore[arg-type]

    assert update_error.value.code == RESOURCE_NOT_FOUND
    assert remove_error.value.code == RESOURCE_NOT_FOUND
