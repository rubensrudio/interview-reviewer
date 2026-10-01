"""Integration tests for requirement list editing and plan confirmation (CT-38)."""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import (
    INVALID_STATE,
    NO_REQUIRED_SKILLS,
    PENDING_CLARIFICATION,
    TOO_MANY_REQUIRED_SKILLS,
    VALIDATION_ERROR,
    AppError,
)
from app.interviews.requirement_list import (
    PREPARE_QUESTIONS_JOB,
    ConfirmationError,
    PlanProposal,
    RequirementItemInput,
    confirm_plan,
    confirm_requirement_list,
    confirmation_errors,
    merge_items,
    replace_requirement_list,
    split_item,
)
from app.interviews.views import ProposalView
from app.models.account import User
from app.models.interview import (
    ExpectedLevel,
    InterviewSession,
    RequirementItem,
    SessionStatus,
)
from app.models.job import Job, JobStatus


def _item(
    item_id: str,
    name: str,
    classification: str = "required",
    level: ExpectedLevel | None = None,
    pending: bool = False,
    terms: list[str] | None = None,
) -> dict[str, Any]:
    return RequirementItem(
        id=item_id,
        name=name,
        original_terms=terms if terms is not None else [name],
        classification=classification,  # type: ignore[arg-type]
        level=level,
        pending_clarification=pending,
        clarification_question="Is it required?" if pending else None,
    ).model_dump(mode="json")


def _required(count: int) -> list[dict[str, Any]]:
    return [_item(f"r{i}", f"Skill {i}") for i in range(count)]


def _session(
    db: Session,
    items: list[dict[str, Any]],
    status: SessionStatus = SessionStatus.AWAITING_CONFIRMATION,
    interview_level: ExpectedLevel | None = None,
    snapshot_minimal: bool = False,
) -> InterviewSession:
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    snapshot: list[dict[str, Any]] = [
        {
            "id": "s1",
            "kind": "skill",
            "fields": {"name": "Python"},
            "origin": "explicit",
            "evidence": ["Python"],
        }
    ]
    session = InterviewSession(
        user_id=user.id,
        status=status,
        interview_level=interview_level,
        requirement_items=items,
        snapshot=snapshot,
        snapshot_minimal=snapshot_minimal,
    )
    db.add(session)
    db.flush()
    return session


def _codes(errors: list[ConfirmationError]) -> list[str]:
    return [error.code for error in errors]


def _stored(session: InterviewSession) -> list[RequirementItem]:
    return [RequirementItem.model_validate(item) for item in session.requirement_items]


def _prepare_jobs(db: Session, session: InterviewSession) -> list[Job]:
    jobs = db.execute(select(Job).where(Job.kind == PREPARE_QUESTIONS_JOB)).scalars().all()
    return [job for job in jobs if job.payload == {"session_id": str(session.id)}]


# --- confirmation_errors (PLAN-04, PLAN-07, PLAN-08, PLAN-91) ---------------------------------


def test_plan_07_no_required_skills_blocks_confirmation() -> None:
    items = [RequirementItem.model_validate(_item("a", "Go", "nice_to_have"))]
    errors = confirmation_errors(items, 20)
    assert _codes(errors) == [NO_REQUIRED_SKILLS]


def test_plan_07_empty_list_blocks_confirmation() -> None:
    assert _codes(confirmation_errors([], 20)) == [NO_REQUIRED_SKILLS]


def test_plan_08_21_required_reports_count_and_excess() -> None:
    items = [RequirementItem.model_validate(item) for item in _required(21)]
    errors = confirmation_errors(items, 20)
    assert _codes(errors) == [TOO_MANY_REQUIRED_SKILLS]
    assert errors[0].details == {"count": 21, "excess": 1}


def test_plan_91_exactly_20_required_is_accepted() -> None:
    items = [RequirementItem.model_validate(item) for item in _required(20)]
    assert confirmation_errors(items, 20) == []


def test_plan_08_counts_distinct_required_skills() -> None:
    items = [RequirementItem.model_validate(item) for item in _required(20)]
    items.append(RequirementItem.model_validate(_item("dup", " skill 3 ")))
    assert confirmation_errors(items, 20) == []


def test_plan_04_pending_item_blocks_confirmation() -> None:
    items = [
        RequirementItem.model_validate(_item("a", "Python")),
        RequirementItem.model_validate(_item("b", "Rust", pending=True)),
    ]
    assert _codes(confirmation_errors(items, 20)) == [PENDING_CLARIFICATION]


# --- confirm_requirement_list (PLAN-09) ----------------------------------------------------


@pytest.mark.parametrize("required_count", [1, 5, 20])
def test_plan_09_proposal_has_one_question_per_required_skill(
    db: Session, required_count: int
) -> None:
    items = [*_required(required_count), _item("n1", "Kafka", "nice_to_have")]
    session = _session(db, items)

    proposal = confirm_requirement_list(db, session)

    assert isinstance(proposal, PlanProposal)
    assert proposal.planned_count == required_count
    assert proposal.skills == [f"Skill {i}" for i in range(required_count)]
    assert session.proposal == {
        "planned_count": required_count,
        "skills": proposal.skills,
    }
    # Shape read by the session view (CT-40).
    view = ProposalView(**session.proposal)
    assert view.planned_count == required_count
    assert session.status == SessionStatus.AWAITING_CONFIRMATION
    assert session.planned_count is None


def test_plan_08_confirm_list_with_21_required_raises(db: Session) -> None:
    session = _session(db, _required(21))
    with pytest.raises(AppError) as exc_info:
        confirm_requirement_list(db, session)
    assert exc_info.value.code == TOO_MANY_REQUIRED_SKILLS
    assert exc_info.value.details == {"count": 21, "excess": 1}
    assert "21" in exc_info.value.message
    assert session.proposal is None


def test_plan_07_confirm_list_without_required_raises(db: Session) -> None:
    session = _session(db, [_item("a", "Go", "nice_to_have")])
    with pytest.raises(AppError) as exc_info:
        confirm_requirement_list(db, session)
    assert exc_info.value.code == NO_REQUIRED_SKILLS


def test_plan_04_confirm_list_with_pending_raises(db: Session) -> None:
    session = _session(db, [_item("a", "Python"), _item("b", "Rust", pending=True)])
    with pytest.raises(AppError) as exc_info:
        confirm_requirement_list(db, session)
    assert exc_info.value.code == PENDING_CLARIFICATION
    assert session.proposal is None


def test_confirm_list_outside_awaiting_confirmation_is_invalid_state(db: Session) -> None:
    session = _session(db, _required(2), status=SessionStatus.COLLECTING_REQUIREMENTS)
    with pytest.raises(AppError) as exc_info:
        confirm_requirement_list(db, session)
    assert exc_info.value.code == INVALID_STATE


def test_plan_09_works_with_minimal_snapshot(db: Session) -> None:
    session = _session(db, _required(3), snapshot_minimal=True)
    proposal = confirm_requirement_list(db, session)
    assert proposal.planned_count == 3
    confirm_plan(db, session)
    assert session.status == SessionStatus.PREPARING_QUESTIONS


# --- replace_requirement_list (PLAN-05, PLAN-11) ---------------------------------------------


def test_plan_05_add_edit_remove_and_reclassify(db: Session) -> None:
    session = _session(db, [_item("a", "Python"), _item("b", "Java"), _item("c", "Go")])

    replace_requirement_list(
        db,
        session,
        [
            RequirementItemInput(
                id="a", name="Python 3", original_terms=["Python"], classification="required"
            ),
            RequirementItemInput(
                id="c",
                name="Go",
                original_terms=["Go"],
                classification="nice_to_have",
                level=ExpectedLevel.SENIOR,
            ),
            RequirementItemInput(name="SQL", classification="required"),
        ],
    )

    stored = _stored(session)
    assert [item.name for item in stored] == ["Python 3", "Go", "SQL"]
    assert stored[0].id == "a"
    assert stored[1].classification == "nice_to_have"
    assert stored[1].level == ExpectedLevel.SENIOR
    assert stored[2].id not in {"a", "b", "c"}
    assert stored[2].original_terms == ["SQL"]
    assert session.status == SessionStatus.AWAITING_CONFIRMATION


def test_plan_05_edit_clears_stale_proposal(db: Session) -> None:
    session = _session(db, _required(2))
    confirm_requirement_list(db, session)
    assert session.proposal is not None

    replace_requirement_list(
        db, session, [RequirementItemInput(id="r0", name="Skill 0", classification="required")]
    )

    assert session.proposal is None
    assert confirm_requirement_list(db, session).planned_count == 1


def test_plan_04_unchanged_pending_item_stays_pending(db: Session) -> None:
    session = _session(db, [_item("a", "Python"), _item("b", "Rust", pending=True)])

    replace_requirement_list(
        db,
        session,
        [
            RequirementItemInput(id="a", name="Python", classification="required"),
            RequirementItemInput(
                id="b",
                name="Rust",
                classification="required",
                level=ExpectedLevel.JUNIOR,
            ),
        ],
    )

    stored = _stored(session)
    assert stored[1].pending_clarification is True
    assert stored[1].clarification_question == "Is it required?"
    with pytest.raises(AppError) as exc_info:
        confirm_requirement_list(db, session)
    assert exc_info.value.code == PENDING_CLARIFICATION


def test_plan_04_reclassifying_pending_item_resolves_it(db: Session) -> None:
    session = _session(db, [_item("a", "Python"), _item("b", "Rust", pending=True)])

    replace_requirement_list(
        db,
        session,
        [
            RequirementItemInput(id="a", name="Python", classification="required"),
            RequirementItemInput(id="b", name="Rust", classification="nice_to_have"),
        ],
    )

    stored = _stored(session)
    assert stored[1].pending_clarification is False
    assert stored[1].clarification_question is None
    assert confirm_requirement_list(db, session).skills == ["Python"]


def test_replace_rejects_duplicate_ids(db: Session) -> None:
    session = _session(db, [_item("a", "Python")])
    with pytest.raises(AppError) as exc_info:
        replace_requirement_list(
            db,
            session,
            [
                RequirementItemInput(id="a", name="Python", classification="required"),
                RequirementItemInput(id="a", name="Go", classification="required"),
            ],
        )
    assert exc_info.value.code == VALIDATION_ERROR
    assert _stored(session)[0].name == "Python"


def test_requirement_item_input_rejects_blank_name() -> None:
    with pytest.raises(ValidationError):
        RequirementItemInput(name="   ", classification="required")


def test_replace_outside_awaiting_confirmation_is_invalid_state(db: Session) -> None:
    session = _session(db, [], status=SessionStatus.COLLECTING_REQUIREMENTS)
    with pytest.raises(AppError) as exc_info:
        replace_requirement_list(
            db, session, [RequirementItemInput(name="Python", classification="required")]
        )
    assert exc_info.value.code == INVALID_STATE
    assert session.requirement_items == []


def test_plan_11_merge_keeps_original_terms() -> None:
    items = [
        RequirementItem.model_validate(_item("a", "Postgres")),
        RequirementItem.model_validate(_item("b", "PostgreSQL", level=ExpectedLevel.SENIOR)),
        RequirementItem.model_validate(_item("c", "Go")),
    ]

    merged = merge_items(items, ["a", "b"], "PostgreSQL")

    assert [item.name for item in merged] == ["PostgreSQL", "Go"]
    assert merged[0].original_terms == ["Postgres", "PostgreSQL"]
    assert merged[0].classification == "required"
    assert merged[0].level == ExpectedLevel.SENIOR


def test_plan_11_unmerge_item_with_two_terms_gives_two_items() -> None:
    items = [
        RequirementItem.model_validate(
            _item(
                "m", "PostgreSQL", terms=["Postgres", "PostgreSQL"], level=ExpectedLevel.MID_LEVEL
            )
        ),
        RequirementItem.model_validate(_item("c", "Go")),
    ]

    split = split_item(items, "m")

    assert len(split) == 3
    assert [item.name for item in split[:2]] == ["Postgres", "PostgreSQL"]
    assert [item.original_terms for item in split[:2]] == [["Postgres"], ["PostgreSQL"]]
    assert all(item.classification == "required" for item in split[:2])
    assert all(item.level == ExpectedLevel.MID_LEVEL for item in split[:2])
    assert len({item.id for item in split}) == 3
    assert split[2].name == "Go"


def test_plan_11_unmerge_round_trips_through_replace(db: Session) -> None:
    session = _session(db, [_item("m", "PostgreSQL", terms=["Postgres", "PostgreSQL"])])
    split = split_item(_stored(session), "m")

    replace_requirement_list(
        db, session, [RequirementItemInput.model_validate(item.model_dump()) for item in split]
    )

    assert confirm_requirement_list(db, session).planned_count == 2


def test_plan_11_merge_and_split_unknown_id_is_validation_error() -> None:
    items = [RequirementItem.model_validate(_item("a", "Go"))]
    with pytest.raises(AppError) as merge_exc:
        merge_items(items, ["a", "zzz"], "Go")
    assert merge_exc.value.code == VALIDATION_ERROR
    with pytest.raises(AppError) as split_exc:
        split_item(items, "zzz")
    assert split_exc.value.code == VALIDATION_ERROR


# --- confirm_plan (PLAN-10, LANG-02) ---------------------------------------------------------


def test_plan_10_confirm_plan_fixes_count_and_enqueues_preparation(db: Session) -> None:
    session = _session(db, _required(3))
    confirm_requirement_list(db, session)

    confirm_plan(db, session)

    assert session.status == SessionStatus.PREPARING_QUESTIONS
    assert session.planned_count == 3
    jobs = _prepare_jobs(db, session)
    assert len(jobs) == 1
    assert jobs[0].status == JobStatus.QUEUED


def test_plan_10_list_cannot_change_after_plan_confirmed(db: Session) -> None:
    session = _session(db, _required(3))
    confirm_requirement_list(db, session)
    confirm_plan(db, session)

    with pytest.raises(AppError) as exc_info:
        replace_requirement_list(
            db, session, [RequirementItemInput(name="Rust", classification="required")]
        )

    assert exc_info.value.code == INVALID_STATE
    assert session.planned_count == 3
    assert session.proposal == {"planned_count": 3, "skills": ["Skill 0", "Skill 1", "Skill 2"]}
    assert len(_stored(session)) == 3


def test_plan_10_confirm_plan_without_proposal_is_invalid_state(db: Session) -> None:
    session = _session(db, _required(3))
    with pytest.raises(AppError) as exc_info:
        confirm_plan(db, session)
    assert exc_info.value.code == INVALID_STATE
    assert session.status == SessionStatus.AWAITING_CONFIRMATION
    assert _prepare_jobs(db, session) == []


def test_plan_10_confirm_plan_twice_is_invalid_state(db: Session) -> None:
    session = _session(db, _required(1))
    confirm_requirement_list(db, session)
    confirm_plan(db, session)
    with pytest.raises(AppError) as exc_info:
        confirm_plan(db, session)
    assert exc_info.value.code == INVALID_STATE
    assert len(_prepare_jobs(db, session)) == 1


def test_lang_02_interview_level_fills_only_items_without_level(db: Session) -> None:
    session = _session(
        db,
        [
            _item("a", "Python"),
            _item("b", "Go", level=ExpectedLevel.JUNIOR),
            _item("c", "Kafka", "nice_to_have"),
        ],
        interview_level=ExpectedLevel.SENIOR,
    )
    confirm_requirement_list(db, session)

    confirm_plan(db, session)

    levels = {item.name: item.level for item in _stored(session)}
    assert levels == {
        "Python": ExpectedLevel.SENIOR,
        "Go": ExpectedLevel.JUNIOR,
        "Kafka": ExpectedLevel.SENIOR,
    }


def test_lang_02_without_interview_level_keeps_items_without_level(db: Session) -> None:
    session = _session(db, [_item("a", "Python")])
    confirm_requirement_list(db, session)
    confirm_plan(db, session)
    assert _stored(session)[0].level is None
