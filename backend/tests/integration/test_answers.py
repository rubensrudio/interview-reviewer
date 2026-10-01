"""Integration tests for idempotent answer submission (CT-41)."""

import threading
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_sessionmaker
from app.errors import (
    ANSWER_TOO_LONG,
    EMPTY_ANSWER,
    INVALID_STATE,
    NOT_CURRENT_QUESTION,
    QUESTION_ALREADY_ANSWERED,
    RESOURCE_NOT_FOUND,
    SESSION_CLOSED,
    AppError,
)
from app.interviews.answers import EVALUATE_JOB_KIND, submit_answer
from app.models.account import User
from app.models.assessment import Answer
from app.models.interview import InterviewSession, Question, SessionStatus
from app.models.job import Job

# --- helpers -------------------------------------------------------------------------------


def _user(db: Session) -> User:
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    return user


def _session(
    db: Session,
    user: User,
    status: SessionStatus = SessionStatus.IN_INTERVIEW,
    planned: int = 3,
) -> tuple[InterviewSession, list[Question]]:
    session = InterviewSession(user_id=user.id, status=status, planned_count=planned)
    db.add(session)
    db.flush()
    questions = [
        Question(
            session_id=session.id,
            position=position,
            skill_name=f"Skill {position}",
            text=f"Question {position}?",
        )
        for position in range(1, planned + 1)
    ]
    db.add_all(questions)
    db.flush()
    return session, questions


def _answers(db: Session, session_id: uuid.UUID) -> list[Answer]:
    return list(db.execute(select(Answer).where(Answer.session_id == session_id)).scalars().all())


def _evaluate_jobs(db: Session, session_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count())
        .select_from(Job)
        .where(Job.kind == EVALUATE_JOB_KIND, Job.payload["session_id"].astext == str(session_id))
    ).scalar_one()


def _key() -> str:
    return uuid.uuid4().hex


def _code(exc: pytest.ExceptionInfo[AppError]) -> str:
    return exc.value.code


# --- INTV-03 / INTV-07: accepted answers ---------------------------------------------------


def test_intv_03_valid_answer_increments_answered_count_by_exactly_one(db: Session) -> None:
    session, questions = _session(db, _user(db))

    submit_answer(db, session, questions[0].id, "The GIL serializes bytecode.", _key())

    assert session.answered_count == 1
    answers = _answers(db, session.id)
    assert len(answers) == 1
    assert answers[0].question_id == questions[0].id
    assert answers[0].content == "The GIL serializes bytecode."
    assert session.status is SessionStatus.IN_INTERVIEW


def test_intv_03_answer_touches_activity(db: Session) -> None:
    session, questions = _session(db, _user(db))
    session.last_activity_at = session.last_activity_at.replace(year=2000)
    db.flush()

    submit_answer(db, session, questions[0].id, "answer", _key())

    assert session.last_activity_at.year > 2000


def test_intv_07_dont_know_is_accepted_and_counted(db: Session) -> None:
    session, questions = _session(db, _user(db))

    submit_answer(db, session, questions[0].id, "I don't know", _key())

    assert session.answered_count == 1
    assert [a.content for a in _answers(db, session.id)] == ["I don't know"]


def test_intv_03_nul_and_lone_surrogates_are_removed_before_storage(db: Session) -> None:
    session, questions = _session(db, _user(db))

    submit_answer(db, session, questions[0].id, "a\x00b\ud800c", _key())

    assert [a.content for a in _answers(db, session.id)] == ["abc"]


# --- INTV-06: idempotency ------------------------------------------------------------------


def test_intv_06_same_submission_twice_records_one_answer(db: Session) -> None:
    session, questions = _session(db, _user(db))
    key = _key()

    submit_answer(db, session, questions[0].id, "answer", key)
    submit_answer(db, session, questions[0].id, "answer", key)

    assert session.answered_count == 1
    assert len(_answers(db, session.id)) == 1


def test_intv_06_replay_after_last_answer_does_not_fail_nor_enqueue_twice(db: Session) -> None:
    session, questions = _session(db, _user(db), planned=1)
    key = _key()

    submit_answer(db, session, questions[0].id, "answer", key)
    submit_answer(db, session, questions[0].id, "answer", key)

    assert session.status is SessionStatus.EVALUATING
    assert session.answered_count == 1
    assert _evaluate_jobs(db, session.id) == 1


# --- INTV-04 / INTV-05: content validation ---------------------------------------------------


@pytest.mark.parametrize("content", ["", "   ", "\n\t  ", "\x00 \x00"])
def test_intv_04_empty_or_blank_answer_is_rejected(db: Session, content: str) -> None:
    session, questions = _session(db, _user(db))

    with pytest.raises(AppError) as exc:
        submit_answer(db, session, questions[0].id, content, _key())

    assert _code(exc) == EMPTY_ANSWER
    assert session.answered_count == 0
    assert _answers(db, session.id) == []


def test_intv_05_answer_over_limit_is_rejected_with_limit(db: Session) -> None:
    session, questions = _session(db, _user(db))
    limit = get_settings().max_answer_chars

    with pytest.raises(AppError) as exc:
        submit_answer(db, session, questions[0].id, "x" * (limit + 1), _key())

    assert _code(exc) == ANSWER_TOO_LONG
    assert exc.value.details == {"limit": limit}
    assert session.answered_count == 0
    assert _answers(db, session.id) == []


def test_intv_05_default_limit_is_5000_and_exact_limit_is_accepted(db: Session) -> None:
    session, questions = _session(db, _user(db))
    assert get_settings().max_answer_chars == 5000

    submit_answer(db, session, questions[0].id, "x" * 5000, _key())

    assert session.answered_count == 1


def test_intv_05_5001_characters_is_rejected(db: Session) -> None:
    session, questions = _session(db, _user(db))

    with pytest.raises(AppError) as exc:
        submit_answer(db, session, questions[0].id, "x" * 5001, _key())

    assert _code(exc) == ANSWER_TOO_LONG


def test_intv_05_limit_is_configurable(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    session, questions = _session(db, _user(db))
    monkeypatch.setenv("IR_MAX_ANSWER_CHARS", "10")
    get_settings.cache_clear()
    try:
        with pytest.raises(AppError) as exc:
            submit_answer(db, session, questions[0].id, "x" * 11, _key())
    finally:
        monkeypatch.delenv("IR_MAX_ANSWER_CHARS")
        get_settings.cache_clear()

    assert _code(exc) == ANSWER_TOO_LONG
    assert exc.value.details == {"limit": 10}


@pytest.mark.parametrize("content", [None, 42, b"bytes"])
def test_intv_04_non_text_answer_is_rejected(db: Session, content: object) -> None:
    session, questions = _session(db, _user(db))

    with pytest.raises(AppError) as exc:
        submit_answer(db, session, questions[0].id, content, _key())  # type: ignore[arg-type]

    assert _code(exc) == EMPTY_ANSWER


# --- INTV-09 / INTV-90 / INTV-91: question checks ------------------------------------------


def test_intv_09_answered_question_cannot_be_replaced(db: Session) -> None:
    session, questions = _session(db, _user(db))
    submit_answer(db, session, questions[0].id, "first", _key())

    with pytest.raises(AppError) as exc:
        submit_answer(db, session, questions[0].id, "second", _key())

    assert _code(exc) == QUESTION_ALREADY_ANSWERED
    assert session.answered_count == 1
    assert [a.content for a in _answers(db, session.id)] == ["first"]


def test_intv_91_answer_to_question_3_when_current_is_2_is_rejected(db: Session) -> None:
    session, questions = _session(db, _user(db))
    submit_answer(db, session, questions[0].id, "first", _key())

    with pytest.raises(AppError) as exc:
        submit_answer(db, session, questions[2].id, "third", _key())

    assert _code(exc) == NOT_CURRENT_QUESTION
    assert session.answered_count == 1
    assert len(_answers(db, session.id)) == 1


def test_intv_91_question_of_another_session_is_not_found(db: Session) -> None:
    user = _user(db)
    session, _ = _session(db, user)
    other_user = _user(db)
    _, other_questions = _session(db, other_user)

    with pytest.raises(AppError) as exc:
        submit_answer(db, session, other_questions[0].id, "answer", _key())

    assert _code(exc) == RESOURCE_NOT_FOUND
    assert session.answered_count == 0
    assert _answers(db, session.id) == []


def test_intv_91_unknown_or_malformed_question_id_is_not_found(db: Session) -> None:
    session, _ = _session(db, _user(db))

    for question_id in (uuid.uuid4(), "not-a-uuid"):
        with pytest.raises(AppError) as exc:
            submit_answer(db, session, question_id, "answer", _key())  # type: ignore[arg-type]
        assert _code(exc) == RESOURCE_NOT_FOUND


@pytest.mark.parametrize("key", ["", "k" * 65, None])
def test_intv_06_invalid_idempotency_key_is_rejected(db: Session, key: object) -> None:
    session, questions = _session(db, _user(db))

    with pytest.raises(AppError):
        submit_answer(db, session, questions[0].id, "answer", key)  # type: ignore[arg-type]

    assert _answers(db, session.id) == []


# --- INTV-11: last answer --------------------------------------------------------------------


def test_intv_11_last_answer_moves_to_evaluating_and_enqueues_evaluation(db: Session) -> None:
    session, questions = _session(db, _user(db), planned=2)

    submit_answer(db, session, questions[0].id, "one", _key())
    assert session.status is SessionStatus.IN_INTERVIEW
    assert _evaluate_jobs(db, session.id) == 0

    submit_answer(db, session, questions[1].id, "two", _key())

    assert session.answered_count == 2
    assert session.status is SessionStatus.EVALUATING
    assert _evaluate_jobs(db, session.id) == 1


# --- INTV-93 and other states ----------------------------------------------------------------


@pytest.mark.parametrize(
    "status", [SessionStatus.CANCELLED, SessionStatus.EXPIRED, SessionStatus.COMPLETED]
)
def test_intv_93_terminal_session_rejects_answer(db: Session, status: SessionStatus) -> None:
    session, questions = _session(db, _user(db), status=status)

    with pytest.raises(AppError) as exc:
        submit_answer(db, session, questions[0].id, "answer", _key())

    assert _code(exc) == SESSION_CLOSED
    assert _answers(db, session.id) == []


@pytest.mark.parametrize(
    "status",
    [
        SessionStatus.COLLECTING_REQUIREMENTS,
        SessionStatus.AWAITING_CONFIRMATION,
        SessionStatus.PREPARING_QUESTIONS,
        SessionStatus.PREPARATION_FAILED,
        SessionStatus.EVALUATING,
        SessionStatus.EVALUATION_FAILED,
    ],
)
def test_intv_93_non_interview_state_is_invalid(db: Session, status: SessionStatus) -> None:
    session, questions = _session(db, _user(db), status=status)

    with pytest.raises(AppError) as exc:
        submit_answer(db, session, questions[0].id, "answer", _key())

    assert _code(exc) == INVALID_STATE
    assert _answers(db, session.id) == []


def test_intv_06_replay_is_a_no_op_even_after_session_closed(db: Session) -> None:
    session, questions = _session(db, _user(db))
    key = _key()
    submit_answer(db, session, questions[0].id, "answer", key)
    session.status = SessionStatus.CANCELLED
    db.flush()

    submit_answer(db, session, questions[0].id, "answer", key)

    assert len(_answers(db, session.id)) == 1


# --- INTV-90 / INTV-06: concurrency with real commits ----------------------------------------


@pytest.fixture
def committed_users(migrated_database: str) -> Iterator[list[uuid.UUID]]:
    user_ids: list[uuid.UUID] = []
    yield user_ids
    with get_sessionmaker()() as cleanup:
        cleanup.execute(delete(User).where(User.id.in_(user_ids)))
        cleanup.commit()


def _committed_session(committed_users: list[uuid.UUID]) -> tuple[uuid.UUID, uuid.UUID]:
    with get_sessionmaker()() as setup:
        user = _user(setup)
        session, questions = _session(setup, user)
        setup.commit()
        committed_users.append(user.id)
        return session.id, questions[0].id


def _race(session_id: uuid.UUID, question_id: uuid.UUID, keys: list[str]) -> list[object]:
    barrier = threading.Barrier(len(keys))
    results: list[object] = []
    lock = threading.Lock()

    def worker(key: str, content: str) -> None:
        outcome: object
        try:
            with get_sessionmaker()() as db:
                session = db.get(InterviewSession, session_id)
                assert session is not None
                barrier.wait(timeout=10)
                submit_answer(db, session, question_id, content, key)
                db.commit()
                outcome = "ok"
        except AppError as error:
            outcome = error.code
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
            outcome = error
        with lock:
            results.append(outcome)

    threads = [
        threading.Thread(target=worker, args=(key, f"answer {index}"))
        for index, key in enumerate(keys)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def test_intv_90_two_tabs_different_keys_same_question_accept_only_one(
    committed_users: list[uuid.UUID],
) -> None:
    session_id, question_id = _committed_session(committed_users)

    results = _race(session_id, question_id, [_key(), _key()])

    assert sorted(map(str, results)) == sorted(["ok", QUESTION_ALREADY_ANSWERED])
    with get_sessionmaker()() as check:
        session = check.get(InterviewSession, session_id)
        assert session is not None
        assert session.answered_count == 1
        assert len(_answers(check, session_id)) == 1


def test_intv_06_concurrent_duplicates_with_same_key_record_one_answer(
    committed_users: list[uuid.UUID],
) -> None:
    session_id, question_id = _committed_session(committed_users)
    key = _key()

    results = _race(session_id, question_id, [key, key])

    assert results == ["ok", "ok"]
    with get_sessionmaker()() as check:
        session = check.get(InterviewSession, session_id)
        assert session is not None
        assert session.answered_count == 1
        assert len(_answers(check, session_id)) == 1
