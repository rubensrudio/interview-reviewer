"""Integration tests for the interview session lifecycle service (CT-36)."""

import copy
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import (
    INVALID_STATE,
    LANGUAGE_NOT_SUPPORTED,
    RESOURCE_NOT_FOUND,
    RESUME_NOT_READY,
    SESSION_IN_PROGRESS,
    VALIDATION_ERROR,
    AppError,
)
from app.interviews import sessions as sessions_module
from app.interviews.sessions import (
    cancel_session,
    get_owned_session,
    list_sessions,
    start_session,
    supported_languages,
    touch_activity,
)
from app.models.account import User
from app.models.interview import (
    ExpectedLevel,
    InterviewSession,
    Message,
    MessageKind,
    MessageRole,
    SessionStatus,
)
from app.models.resume import Resume, ResumeStatus

FIRST_MESSAGE = "Please paste the job requirements for the position you are preparing for."

EXTRACTION: list[dict[str, Any]] = [
    {
        "id": "item-1",
        "kind": "skill",
        "fields": {"name": "Python"},
        "origin": "explicit",
        "evidence": ["5 years of Python"],
    },
    {
        "id": "item-2",
        "kind": "experience",
        "fields": {"title": "Backend developer", "organization": "Acme"},
        "origin": "explicit",
        "evidence": ["Backend developer at Acme"],
    },
]


def _user(db: Session) -> User:
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    return user


def _resume(db: Session, user: User, status: ResumeStatus = ResumeStatus.READY) -> Resume:
    resume = Resume(
        user_id=user.id,
        filename="my-cv.pdf",
        status=status,
        extraction=copy.deepcopy(EXTRACTION) if status is ResumeStatus.READY else None,
    )
    db.add(resume)
    db.flush()
    return resume


def _messages(db: Session, session: InterviewSession) -> list[Message]:
    return list(db.execute(select(Message).where(Message.session_id == session.id)).scalars().all())


def _count_sessions(db: Session, user: User) -> int:
    return db.execute(
        select(func.count())
        .select_from(InterviewSession)
        .where(InterviewSession.user_id == user.id)
    ).scalar_one()


# --- supported languages (LANG-01, LAC-08) ----------------------------------------------


def test_lang_01_supported_languages_is_english_only() -> None:
    assert supported_languages() == ["en"]


# --- start (PLAN-01, CV-11, CV-12, LANG-02) ---------------------------------------------


def test_plan_01_start_with_ready_version_creates_collecting_session(db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user)

    session = start_session(db, user, resume.id, "en", ExpectedLevel.SENIOR)

    assert session.status is SessionStatus.COLLECTING_REQUIREMENTS
    assert session.user_id == user.id
    assert session.resume_id == resume.id
    assert session.resume_name == "my-cv.pdf"
    assert session.language == "en"
    assert session.interview_level is ExpectedLevel.SENIOR
    assert session.snapshot == EXTRACTION
    messages = _messages(db, session)
    assert len(messages) == 1
    assert messages[0].role is MessageRole.ASSISTANT
    assert messages[0].kind is MessageKind.INFO
    assert messages[0].content == FIRST_MESSAGE


def test_lang_02_interview_level_is_optional(db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user)

    session = start_session(db, user, resume.id, "en", None)

    assert session.interview_level is None


def test_lang_02_interview_level_accepts_its_string_value(db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user)

    session = start_session(db, user, resume.id, "en", "mid-level")  # type: ignore[arg-type]

    assert session.interview_level is ExpectedLevel.MID_LEVEL


def test_lang_02_unknown_interview_level_is_rejected(db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user)

    with pytest.raises(AppError) as exc_info:
        start_session(db, user, resume.id, "en", "guru")  # type: ignore[arg-type]

    assert exc_info.value.code == VALIDATION_ERROR
    assert _count_sessions(db, user) == 0


@pytest.mark.parametrize(
    "status", [ResumeStatus.RECEIVED, ResumeStatus.PROCESSING, ResumeStatus.FAILED]
)
def test_cv_11_start_with_version_not_ready_is_rejected(db: Session, status: ResumeStatus) -> None:
    user = _user(db)
    resume = _resume(db, user, status)

    with pytest.raises(AppError) as exc_info:
        start_session(db, user, resume.id, "en", None)

    assert exc_info.value.code == RESUME_NOT_READY
    assert exc_info.value.status == 409
    assert _count_sessions(db, user) == 0


def test_cv_11_start_with_version_of_another_user_is_not_found(db: Session) -> None:
    owner = _user(db)
    other = _user(db)
    resume = _resume(db, owner)

    with pytest.raises(AppError) as exc_info:
        start_session(db, other, resume.id, "en", None)

    assert exc_info.value.code == RESOURCE_NOT_FOUND
    assert _count_sessions(db, other) == 0


@pytest.mark.parametrize("language", ["pt", "EN", "", "en\x00", "\ud800", None, 1])
def test_lang_01_start_with_unsupported_language_is_rejected(db: Session, language: object) -> None:
    user = _user(db)
    resume = _resume(db, user)

    with pytest.raises(AppError) as exc_info:
        start_session(db, user, resume.id, language, None)  # type: ignore[arg-type]

    assert exc_info.value.code == LANGUAGE_NOT_SUPPORTED
    assert exc_info.value.status == 422
    assert _count_sessions(db, user) == 0


def test_cv_12_editing_extraction_after_start_keeps_snapshot(db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user)
    session = start_session(db, user, resume.id, "en", None)

    assert resume.extraction is not None
    resume.extraction[0]["fields"]["name"] = "Rust"
    resume.extraction = [*resume.extraction, {**EXTRACTION[0], "id": "item-3"}]
    db.flush()
    db.expire_all()

    reloaded = db.get(InterviewSession, session.id)
    assert reloaded is not None
    assert reloaded.snapshot == EXTRACTION


# --- one open session per user (PLAN-02) -------------------------------------------------


def test_plan_02_second_session_while_first_in_interview_is_rejected(db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user)
    first = start_session(db, user, resume.id, "en", None)
    first.status = SessionStatus.IN_INTERVIEW
    db.flush()

    with pytest.raises(AppError) as exc_info:
        start_session(db, user, resume.id, "en", None)

    assert exc_info.value.code == SESSION_IN_PROGRESS
    assert exc_info.value.status == 409
    assert exc_info.value.details == {"session_id": str(first.id)}
    assert _count_sessions(db, user) == 1


@pytest.mark.parametrize(
    "terminal", [SessionStatus.COMPLETED, SessionStatus.CANCELLED, SessionStatus.EXPIRED]
)
def test_plan_02_terminal_session_does_not_block_a_new_one(
    db: Session, terminal: SessionStatus
) -> None:
    user = _user(db)
    resume = _resume(db, user)
    first = start_session(db, user, resume.id, "en", None)
    first.status = terminal
    db.flush()

    second = start_session(db, user, resume.id, "en", None)

    assert second.id != first.id
    assert second.status is SessionStatus.COLLECTING_REQUIREMENTS


def test_plan_02_unique_index_violation_becomes_session_in_progress(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(db)
    resume = _resume(db, user)
    first = start_session(db, user, resume.id, "en", None)
    # Simulate a concurrent insert that the pre-check did not see.
    real_lookup = sessions_module._find_open_session_id
    calls: list[int] = []

    def hide_first_lookup(lookup_db: Session, user_id: uuid.UUID) -> uuid.UUID | None:
        calls.append(1)
        return None if len(calls) == 1 else real_lookup(lookup_db, user_id)

    monkeypatch.setattr(sessions_module, "_find_open_session_id", hide_first_lookup)

    with pytest.raises(AppError) as exc_info:
        start_session(db, user, resume.id, "en", None)

    assert exc_info.value.code == SESSION_IN_PROGRESS
    assert exc_info.value.details == {"session_id": str(first.id)}
    # The session is still usable after the failed insert.
    assert _count_sessions(db, user) == 1
    assert len(_messages(db, first)) == 1


def test_plan_02_other_users_open_session_does_not_block(db: Session) -> None:
    user = _user(db)
    other = _user(db)
    start_session(db, other, _resume(db, other).id, "en", None)

    session = start_session(db, user, _resume(db, user).id, "en", None)

    assert session.user_id == user.id


# --- ownership lookup -------------------------------------------------------------------


def test_get_owned_session_returns_own_session(db: Session) -> None:
    user = _user(db)
    session = start_session(db, user, _resume(db, user).id, "en", None)

    assert get_owned_session(db, user, session.id).id == session.id
    assert get_owned_session(db, user, session.id, for_update=True).id == session.id


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "\x00", None, 42])
def test_get_owned_session_malformed_id_is_not_found(db: Session, bad_id: object) -> None:
    user = _user(db)

    with pytest.raises(AppError) as exc_info:
        get_owned_session(db, user, bad_id)  # type: ignore[arg-type]

    assert exc_info.value.code == RESOURCE_NOT_FOUND


def test_get_owned_session_of_another_user_is_not_found(db: Session) -> None:
    owner = _user(db)
    other = _user(db)
    session = start_session(db, owner, _resume(db, owner).id, "en", None)

    with pytest.raises(AppError) as exc_info:
        get_owned_session(db, other, session.id)

    assert exc_info.value.code == RESOURCE_NOT_FOUND
    with pytest.raises(AppError):
        get_owned_session(db, owner, uuid.uuid4())


def test_get_owned_session_for_update_reloads_stale_state(db: Session) -> None:
    user = _user(db)
    session = start_session(db, user, _resume(db, user).id, "en", None)
    db.execute(
        InterviewSession.__table__.update()
        .where(InterviewSession.id == session.id)
        .values(status=SessionStatus.IN_INTERVIEW.value)
    )

    locked = get_owned_session(db, user, session.id, for_update=True)

    assert locked.status is SessionStatus.IN_INTERVIEW


# --- cancel (INTV-12) -------------------------------------------------------------------


def test_intv_12_cancel_moves_to_cancelled_and_second_cancel_is_invalid(db: Session) -> None:
    user = _user(db)
    session = start_session(db, user, _resume(db, user).id, "en", None)

    cancel_session(db, session)
    db.expire_all()
    reloaded = get_owned_session(db, user, session.id)
    assert reloaded.status is SessionStatus.CANCELLED

    with pytest.raises(AppError) as exc_info:
        cancel_session(db, reloaded)

    assert exc_info.value.code == INVALID_STATE
    assert reloaded.status is SessionStatus.CANCELLED


def test_intv_12_cancelled_session_frees_the_slot(db: Session) -> None:
    user = _user(db)
    resume = _resume(db, user)
    session = start_session(db, user, resume.id, "en", None)
    cancel_session(db, session)

    assert start_session(db, user, resume.id, "en", None).id != session.id


# --- activity ---------------------------------------------------------------------------


def test_touch_activity_updates_last_activity(db: Session) -> None:
    user = _user(db)
    session = start_session(db, user, _resume(db, user).id, "en", None)
    session.last_activity_at = datetime.now(UTC) - timedelta(days=10)
    before = datetime.now(UTC)

    touch_activity(session)

    assert session.last_activity_at >= before


# --- history (DATA-01) ------------------------------------------------------------------


def test_data_01_list_sessions_returns_only_own_sessions_newest_first(db: Session) -> None:
    user = _user(db)
    other = _user(db)
    now = datetime.now(UTC)
    statuses = [
        (SessionStatus.COMPLETED, now - timedelta(days=3)),
        (SessionStatus.EXPIRED, now - timedelta(days=40)),
        (SessionStatus.CANCELLED, now - timedelta(days=1)),
        (SessionStatus.IN_INTERVIEW, now),
    ]
    created = []
    for status, created_at in statuses:
        session = InterviewSession(user_id=user.id, status=status, created_at=created_at)
        db.add(session)
        created.append(session)
    db.add(InterviewSession(user_id=other.id, status=SessionStatus.COMPLETED, created_at=now))
    db.flush()

    listed = list_sessions(db, user)

    expected = sorted(created, key=lambda item: item.created_at, reverse=True)
    assert [item.id for item in listed] == [item.id for item in expected]
    assert {item.status for item in listed} >= {SessionStatus.CANCELLED, SessionStatus.EXPIRED}
    assert all(item.user_id == user.id for item in listed)


def test_data_01_list_sessions_is_empty_for_new_user(db: Session) -> None:
    assert list_sessions(db, _user(db)) == []
