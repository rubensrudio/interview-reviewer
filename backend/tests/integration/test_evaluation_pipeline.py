"""Integration tests for the evaluation pipeline job (CT-48).

Covers EVAL-01, EVAL-08, EVAL-11, EVAL-12, EVAL-13, EVAL-14, EVAL-90, EVAL-93, DATA-91 and
KNOW-92. The LLM is always the scripted ``FakeLLM``; tests never reach a real inference server.
"""

import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine
from app.errors import INVALID_STATE, AppError
from app.evaluation import pipeline
from app.evaluation.evaluator import EVALUATION_TASK
from app.evaluation.pipeline import EVALUATE_JOB_KIND, retry_evaluation, run_evaluation
from app.evaluation.reference_answers import REFERENCE_ANSWER_TASK
from app.jobs.registry import default_registry
from app.llm.client import LLMUnavailable
from app.models.account import User
from app.models.assessment import Answer, Evaluation, Report
from app.models.interview import ExpectedLevel, InterviewSession, Question, SessionStatus
from app.models.job import Job
from tests.fakes.fake_llm import FakeLLM

SKILLS = ["Python", "PostgreSQL", "Kubernetes"]
COLLECTED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
ANSWER_MARKER = "secret-answer-marker"


@pytest.fixture(autouse=True)
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("IR_LLM_MAX_ATTEMPTS", "3")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _use_llm(monkeypatch: pytest.MonkeyPatch, llm: FakeLLM) -> None:
    monkeypatch.setattr(pipeline, "get_llm_client", lambda: llm)


def _source(skill: str) -> dict[str, Any]:
    return {
        "url": f"https://docs.example.com/{skill.lower()}",
        "title": f"{skill} documentation",
        "collected_at": COLLECTED_AT.isoformat(),
        "excerpt": f"{skill} reference excerpt",
    }


def _session(
    db: Session,
    answers: list[str] | None = None,
    status: SessionStatus = SessionStatus.EVALUATING,
) -> InterviewSession:
    contents = answers if answers is not None else [f"{ANSWER_MARKER} {s}" for s in SKILLS]
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    session = InterviewSession(
        user_id=user.id,
        status=status,
        interview_level=ExpectedLevel.MID_LEVEL,
        requirement_items=[],
        proposal={"planned_count": len(SKILLS), "skills": SKILLS},
        planned_count=len(SKILLS),
        answered_count=len(SKILLS),
        snapshot=[],
    )
    db.add(session)
    db.flush()
    for position, (skill, content) in enumerate(zip(SKILLS, contents, strict=True), start=1):
        question = Question(
            session_id=session.id,
            position=position,
            skill_name=skill,
            expected_level=ExpectedLevel.MID_LEVEL,
            text=f"Explain {skill}.",
            reference_points=[f"Point about {skill}"],
            sources=[_source(skill)] if position != 3 else [],
            no_verified_source=position == 3,
        )
        db.add(question)
        db.flush()
        db.add(
            Answer(
                session_id=session.id,
                question_id=question.id,
                content=content,
                idempotency_key=f"key-{position}",
            )
        )
    db.flush()
    return session


def _evaluation(score: int = 4) -> dict[str, Any]:
    return {
        "score": score,
        "justification": f"Scored {score} for the content.",
        "evidence_quotes": [],
        "gap_explanation": None if score >= 3 else "Missing key points.",
    }


def _reference(skill: str = "Python") -> dict[str, Any]:
    return {
        "text": f"Reference answer about {skill}.",
        "points": [f"Point about {skill}"],
        "source_urls": [f"https://docs.example.com/{skill.lower()}"],
        "example_text": None,
        "example_evidence": None,
    }


def _run(db: Session, session_id: uuid.UUID) -> InterviewSession | None:
    run_evaluation(db, {"session_id": str(session_id)})
    db.expire_all()
    return db.execute(
        select(InterviewSession).where(InterviewSession.id == session_id)
    ).scalar_one_or_none()


def _evaluations(db: Session, session_id: uuid.UUID) -> dict[int, Evaluation]:
    rows = db.execute(
        select(Question.position, Evaluation)
        .join(Answer, Answer.question_id == Question.id)
        .join(Evaluation, Evaluation.answer_id == Answer.id)
        .where(Question.session_id == session_id)
    ).all()
    return {position: evaluation for position, evaluation in rows}


def _report(db: Session, session_id: uuid.UUID) -> Report | None:
    return db.execute(select(Report).where(Report.session_id == session_id)).scalar_one_or_none()


def _evaluate_jobs(db: Session, session_id: uuid.UUID) -> list[Job]:
    jobs = db.execute(select(Job).where(Job.kind == EVALUATE_JOB_KIND)).scalars().all()
    return [job for job in jobs if job.payload == {"session_id": str(session_id)}]


# --- registration (CT-7, CT-48) ----------------------------------------------------------------


def test_eval_01_handler_is_registered_for_session_evaluate() -> None:
    assert EVALUATE_JOB_KIND == "session.evaluate"
    assert default_registry.handler_for(EVALUATE_JOB_KIND) is run_evaluation


# --- success (EVAL-01, EVAL-11, EVAL-93) -------------------------------------------------------


def test_eval_11_three_answers_evaluated_store_report_and_complete(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(db)
    llm = FakeLLM({EVALUATION_TASK: [_evaluation(4), _evaluation(3), _evaluation(4)]})
    _use_llm(monkeypatch, llm)

    reloaded = _run(db, session.id)

    assert reloaded is not None
    assert reloaded.status == SessionStatus.COMPLETED
    assert reloaded.completed_at is not None
    assert reloaded.evaluation_attempts == 1
    evaluations = _evaluations(db, session.id)
    assert sorted(evaluations) == [1, 2, 3]
    assert [evaluations[p].score for p in (1, 2, 3)] == [4, 3, 4]
    assert all(e.reference_answer is None for e in evaluations.values())
    report = _report(db, session.id)
    assert report is not None
    settings = get_settings()
    expected_version = f"{settings.llm_model}+{settings.llm_config_version}"
    assert report.model_version == expected_version
    assert report.rubric_version == settings.rubric_version
    assert all(e.model_version == expected_version for e in evaluations.values())
    assert report.adherence_percentage == Decimal("91.7")
    assert report.content["adherence_percentage"] == "91.7"
    assert report.content["model_version"] == expected_version
    assert report.content["rubric_version"] == settings.rubric_version
    # The report freezes the completion time of the session.
    assert datetime.fromisoformat(report.content["completed_at"]) == reloaded.completed_at
    assert len(llm.calls_for(EVALUATION_TASK)) == 3
    assert llm.calls_for(REFERENCE_ANSWER_TASK) == []


def test_eval_08_unsatisfactory_item_gets_reference_answer(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(db)
    llm = FakeLLM(
        {
            EVALUATION_TASK: [_evaluation(2), _evaluation(4), _evaluation(1)],
            REFERENCE_ANSWER_TASK: [_reference("Python"), _reference("Kubernetes")],
        }
    )
    _use_llm(monkeypatch, llm)

    reloaded = _run(db, session.id)

    assert reloaded is not None
    assert reloaded.status == SessionStatus.COMPLETED
    evaluations = _evaluations(db, session.id)
    assert evaluations[1].reference_answer is not None
    assert evaluations[1].reference_answer["text"] == "Reference answer about Python."
    assert evaluations[2].reference_answer is None
    assert evaluations[3].reference_answer is not None
    report = _report(db, session.id)
    assert report is not None
    assert report.content["unsatisfactory_items"] == [1, 3]


def test_eval_08_reference_failure_leaves_item_without_evaluation(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(db)
    llm = FakeLLM(
        {
            EVALUATION_TASK: [_evaluation(1), _evaluation(4), _evaluation(4)],
            REFERENCE_ANSWER_TASK: [{"text": ""}] * 3,
        }
    )
    _use_llm(monkeypatch, llm)

    reloaded = _run(db, session.id)

    assert reloaded is not None
    assert reloaded.status == SessionStatus.EVALUATION_FAILED
    evaluations = _evaluations(db, session.id)
    # An unsatisfactory item is never stored without its reference answer.
    assert sorted(evaluations) == [2, 3]
    assert _report(db, session.id) is None


# --- failures (EVAL-13, KNOW-92) ---------------------------------------------------------------


def test_eval_13_item_always_invalid_fails_without_report_nor_zero_score(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(db)
    invalid = {"score": 7, "justification": "x", "evidence_quotes": [], "gap_explanation": None}
    llm = FakeLLM({EVALUATION_TASK: [_evaluation(4), invalid, invalid, invalid, _evaluation(3)]})
    _use_llm(monkeypatch, llm)

    reloaded = _run(db, session.id)

    assert reloaded is not None
    assert reloaded.status == SessionStatus.EVALUATION_FAILED
    assert reloaded.completed_at is None
    assert _report(db, session.id) is None
    evaluations = _evaluations(db, session.id)
    # Valid evaluations are kept for the retry; the failed item gets none (no artificial 0).
    assert sorted(evaluations) == [1, 3]
    answers = db.execute(select(Answer).where(Answer.session_id == session.id)).scalars().all()
    assert len(answers) == 3


def test_know_92_llm_unavailable_fails_evaluation_and_keeps_answers(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(db)
    llm = FakeLLM({EVALUATION_TASK: [LLMUnavailable] * 9})
    _use_llm(monkeypatch, llm)

    reloaded = _run(db, session.id)

    assert reloaded is not None
    assert reloaded.status == SessionStatus.EVALUATION_FAILED
    assert _evaluations(db, session.id) == {}
    assert _report(db, session.id) is None
    answers = db.execute(select(Answer).where(Answer.session_id == session.id)).scalars().all()
    assert len(answers) == 3


def test_know_92_misconfigured_llm_client_fails_evaluation(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(db)

    def broken_client() -> FakeLLM:
        raise ValueError("host not allowed")

    monkeypatch.setattr(pipeline, "get_llm_client", broken_client)

    reloaded = _run(db, session.id)

    assert reloaded is not None
    assert reloaded.status == SessionStatus.EVALUATION_FAILED
    assert _report(db, session.id) is None


# --- retry (EVAL-14) ---------------------------------------------------------------------------


def test_eval_14_retry_reuses_valid_evaluations_and_completes(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(db)
    first = FakeLLM({EVALUATION_TASK: [_evaluation(4), *[LLMUnavailable] * 3, _evaluation(3)]})
    _use_llm(monkeypatch, first)
    failed = _run(db, session.id)
    assert failed is not None
    assert failed.status == SessionStatus.EVALUATION_FAILED
    assert sorted(_evaluations(db, session.id)) == [1, 3]

    retry_evaluation(db, failed)
    assert failed.status == SessionStatus.EVALUATING
    assert len(_evaluate_jobs(db, session.id)) == 1

    second = FakeLLM({EVALUATION_TASK: [_evaluation(2)], REFERENCE_ANSWER_TASK: [_reference()]})
    _use_llm(monkeypatch, second)
    reloaded = _run(db, session.id)

    assert reloaded is not None
    assert reloaded.status == SessionStatus.COMPLETED
    # Only the missing item called the LLM.
    assert len(second.calls_for(EVALUATION_TASK)) == 1
    assert "PostgreSQL" in second.calls_for(EVALUATION_TASK)[0].user
    assert sorted(_evaluations(db, session.id)) == [1, 2, 3]
    assert _report(db, session.id) is not None
    assert reloaded.evaluation_attempts == 2


@pytest.mark.parametrize(
    "status",
    [SessionStatus.EVALUATING, SessionStatus.COMPLETED, SessionStatus.IN_INTERVIEW],
)
def test_eval_14_retry_outside_evaluation_failed_is_rejected(
    db: Session, status: SessionStatus
) -> None:
    session = _session(db, status=status)

    with pytest.raises(AppError) as error:
        retry_evaluation(db, session)

    assert error.value.code == INVALID_STATE
    assert session.status == status
    assert _evaluate_jobs(db, session.id) == []


# --- "I don't know" (EVAL-90) ------------------------------------------------------------------


def test_eval_90_all_dont_know_scores_zero_with_references(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(db, answers=["I don't know"] * 3)
    llm = FakeLLM({REFERENCE_ANSWER_TASK: [_reference(skill) for skill in SKILLS]})
    _use_llm(monkeypatch, llm)

    reloaded = _run(db, session.id)

    assert reloaded is not None
    assert reloaded.status == SessionStatus.COMPLETED
    evaluations = _evaluations(db, session.id)
    assert [evaluations[p].score for p in (1, 2, 3)] == [0, 0, 0]
    assert all(e.reference_answer is not None for e in evaluations.values())
    report = _report(db, session.id)
    assert report is not None
    assert report.adherence_percentage == Decimal("0.0")
    assert report.content["adherence_percentage"] == "0.0"
    assert all(item["reference_answer"] is not None for item in report.content["items"])
    assert all(not item["satisfactory"] for item in report.content["items"])
    assert llm.calls_for(EVALUATION_TASK) == []


# --- deletion during the job (DATA-91) ---------------------------------------------------------


def test_data_91_session_deleted_during_job_persists_nothing(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(db)
    session_id = session.id

    class DeletingLLM(FakeLLM):
        def complete_structured(self, task, system, user, output_model):  # type: ignore[no-untyped-def]
            db.execute(delete(InterviewSession).where(InterviewSession.id == session_id))
            return super().complete_structured(task, system, user, output_model)

    _use_llm(monkeypatch, DeletingLLM({EVALUATION_TASK: [_evaluation(4)] * 3}))

    assert _run(db, session_id) is None
    assert _report(db, session_id) is None
    assert (
        db.execute(
            select(Evaluation)
            .join(Answer, Answer.id == Evaluation.answer_id)
            .where(Answer.session_id == session_id)
        ).all()
        == []
    )


# --- idempotency and skipped states (EVAL-12) --------------------------------------------------


def test_eval_12_rerun_on_completed_session_keeps_report(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(db)
    _use_llm(monkeypatch, FakeLLM({EVALUATION_TASK: [_evaluation(4)] * 3}))
    _run(db, session.id)
    report = _report(db, session.id)
    assert report is not None
    report_id, content = report.id, dict(report.content)

    again = FakeLLM({EVALUATION_TASK: [_evaluation(0)] * 3})
    _use_llm(monkeypatch, again)
    reloaded = _run(db, session.id)

    assert reloaded is not None
    assert reloaded.status == SessionStatus.COMPLETED
    rerun_report = _report(db, session.id)
    assert rerun_report is not None
    assert rerun_report.id == report_id
    assert rerun_report.content == content
    assert again.calls == []


@pytest.mark.parametrize(
    "status",
    [SessionStatus.IN_INTERVIEW, SessionStatus.EVALUATION_FAILED, SessionStatus.CANCELLED],
)
def test_eval_12_session_not_evaluating_is_skipped(
    db: Session, monkeypatch: pytest.MonkeyPatch, status: SessionStatus
) -> None:
    session = _session(db, status=status)
    llm = FakeLLM({EVALUATION_TASK: [_evaluation(4)] * 3})
    _use_llm(monkeypatch, llm)

    reloaded = _run(db, session.id)

    assert reloaded is not None
    assert reloaded.status == status
    assert llm.calls == []
    assert _evaluations(db, session.id) == {}


def test_eval_01_session_left_evaluating_by_another_job_is_not_written(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(db)
    session_id = session.id

    class CancellingLLM(FakeLLM):
        def complete_structured(self, task, system, user, output_model):  # type: ignore[no-untyped-def]
            db.execute(
                InterviewSession.__table__.update()
                .where(InterviewSession.id == session_id)
                .values(status=SessionStatus.CANCELLED.value)
            )
            return super().complete_structured(task, system, user, output_model)

    _use_llm(monkeypatch, CancellingLLM({EVALUATION_TASK: [_evaluation(4)] * 3}))

    reloaded = _run(db, session_id)

    assert reloaded is not None
    assert reloaded.status == SessionStatus.CANCELLED
    assert _evaluations(db, session_id) == {}
    assert _report(db, session_id) is None


def test_eval_01_unknown_session_is_ignored(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeLLM({EVALUATION_TASK: [_evaluation(4)]})
    _use_llm(monkeypatch, llm)

    run_evaluation(db, {"session_id": str(uuid.uuid4())})

    assert llm.calls == []


@pytest.mark.parametrize("payload", [{}, {"session_id": "not-a-uuid"}])
def test_eval_01_invalid_payload_is_rejected(db: Session, payload: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        run_evaluation(db, payload)


def test_eval_01_logs_never_contain_answer_text(
    db: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    session = _session(db)
    _use_llm(monkeypatch, FakeLLM({EVALUATION_TASK: [_evaluation(4)] * 3}))

    with caplog.at_level(logging.DEBUG):
        _run(db, session.id)

    logged = caplog.text + " ".join(str(record.__dict__) for record in caplog.records)
    assert ANSWER_MARKER not in logged
    assert "Scored 4" not in logged


# --- unexpected errors never leave a session in evaluating (KNOW-92) ---------------------------


@pytest.fixture
def committed_session(migrated_database: str) -> Iterator[uuid.UUID]:
    """An evaluating session committed for real, so a fresh session can mark it failed."""
    with Session(get_engine()) as setup:
        session = _session(setup)
        setup.commit()
        session_id, user_id = session.id, session.user_id
    yield session_id
    with Session(get_engine()) as cleanup:
        cleanup.execute(delete(User).where(User.id == user_id))
        cleanup.commit()


def _committed_status(session_id: uuid.UUID) -> SessionStatus:
    with Session(get_engine()) as reader:
        return reader.execute(
            select(InterviewSession.status).where(InterviewSession.id == session_id)
        ).scalar_one()


def test_know_92_unexpected_error_marks_evaluation_failed(
    committed_session: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_llm(monkeypatch, FakeLLM({EVALUATION_TASK: [_evaluation(4)] * 3}))

    def broken_builder(*args: object) -> None:
        raise RuntimeError("builder failed")

    monkeypatch.setattr(pipeline, "build_report_content", broken_builder)

    with Session(get_engine()) as worker, pytest.raises(RuntimeError):
        run_evaluation(worker, {"session_id": str(committed_session)})

    assert _committed_status(committed_session) == SessionStatus.EVALUATION_FAILED
