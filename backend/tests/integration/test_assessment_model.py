"""Tests for the answers, evaluations and reports tables (migration 0006_assessments)."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, func, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.models import Answer as ExportedAnswer
from app.models import Evaluation as ExportedEvaluation
from app.models import Report as ExportedReport
from app.models import User
from app.models.assessment import Answer, Evaluation, Report
from app.models.interview import InterviewSession, Question, SessionStatus
from app.models.knowledge import SourceRef

COLLECTED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
ASSESSMENT_TABLES = {"answers", "evaluations", "reports"}


def _session(db: Session) -> InterviewSession:
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    session = InterviewSession(user_id=user.id, status=SessionStatus.IN_INTERVIEW)
    db.add(session)
    db.flush()
    return session


def _question(db: Session, session: InterviewSession, position: int = 1) -> Question:
    question = Question(
        session_id=session.id, position=position, skill_name="Python", text="Explain the GIL."
    )
    db.add(question)
    db.flush()
    return question


def _answer(
    db: Session, session: InterviewSession, question: Question, key: str | None = None
) -> Answer:
    answer = Answer(
        session_id=session.id,
        question_id=question.id,
        content="It is a global lock.",
        idempotency_key=key or uuid.uuid4().hex,
    )
    db.add(answer)
    db.flush()
    return answer


def _evaluation(db: Session, answer: Answer, **overrides: Any) -> Evaluation:
    data: dict[str, Any] = {
        "answer_id": answer.id,
        "score": 3,
        "justification": "Mentions the lock.",
        "model_version": "model-v1",
    }
    data.update(overrides)
    evaluation = Evaluation(**data)
    db.add(evaluation)
    db.flush()
    return evaluation


def _report(db: Session, session: InterviewSession) -> Report:
    report = Report(
        session_id=session.id,
        content={"summary": "Good", "adherence_percentage": "62.5"},
        adherence_percentage=Decimal("62.5"),
        model_version="model-v1",
        rubric_version="rubric-v1",
    )
    db.add(report)
    db.flush()
    return report


def test_models_package_exports_assessment_models() -> None:
    assert ExportedAnswer is Answer
    assert ExportedEvaluation is Evaluation
    assert ExportedReport is Report


def test_assessment_tables_exist(db: Session) -> None:
    assert ASSESSMENT_TABLES <= set(inspect(db.connection()).get_table_names())


# --- answers ---


def test_answer_is_persisted(db: Session) -> None:
    session = _session(db)
    question = _question(db, session)
    answer = _answer(db, session, question, key="key-1")
    db.commit()
    answer_id, session_id, question_id = answer.id, session.id, question.id
    db.expunge_all()

    loaded = db.get(Answer, answer_id)
    assert loaded is not None
    assert isinstance(loaded.id, uuid.UUID)
    assert loaded.session_id == session_id
    assert loaded.question_id == question_id
    assert loaded.content == "It is a global lock."
    assert loaded.idempotency_key == "key-1"
    assert loaded.created_at.tzinfo is not None


def test_intv_09_second_answer_for_same_question_is_rejected(db: Session) -> None:
    session = _session(db)
    question = _question(db, session)
    _answer(db, session, question)
    with pytest.raises(IntegrityError):
        _answer(db, session, question)


def test_intv_06_same_idempotency_key_in_session_is_rejected(db: Session) -> None:
    session = _session(db)
    first = _question(db, session, position=1)
    second = _question(db, session, position=2)
    _answer(db, session, first, key="same-key")
    with pytest.raises(IntegrityError):
        _answer(db, session, second, key="same-key")


def test_intv_06_same_idempotency_key_in_other_session_is_accepted(db: Session) -> None:
    session_a = _session(db)
    session_b = _session(db)
    _answer(db, session_a, _question(db, session_a), key="same-key")
    _answer(db, session_b, _question(db, session_b), key="same-key")


def test_intv_09_update_on_answer_is_rejected_by_database(db: Session) -> None:
    session = _session(db)
    answer = _answer(db, session, _question(db, session))
    with pytest.raises(DBAPIError, match="answers_immutable"):
        db.execute(update(Answer).where(Answer.id == answer.id).values(content="Edited"))


def test_answer_requires_existing_question(db: Session) -> None:
    session = _session(db)
    db.add(
        Answer(session_id=session.id, question_id=uuid.uuid4(), content="x", idempotency_key="k")
    )
    with pytest.raises(IntegrityError):
        db.flush()


# --- evaluations ---


def test_evaluation_is_persisted_with_defaults(db: Session) -> None:
    session = _session(db)
    answer = _answer(db, session, _question(db, session))
    evaluation = _evaluation(db, answer)
    db.commit()
    evaluation_id = evaluation.id
    db.expunge_all()

    loaded = db.get(Evaluation, evaluation_id)
    assert loaded is not None
    assert loaded.score == 3
    assert loaded.justification == "Mentions the lock."
    assert loaded.evidence_quotes == []
    assert loaded.gap_explanation is None
    assert loaded.reference_answer is None
    assert loaded.model_version == "model-v1"
    assert loaded.created_at.tzinfo is not None


def test_evaluation_persists_reference_answer(db: Session) -> None:
    session = _session(db)
    answer = _answer(db, session, _question(db, session))
    source = SourceRef(
        url="https://docs.python.org/3/", title="Python", collected_at=COLLECTED_AT, excerpt="x"
    )
    reference = {
        "text": "The GIL serializes bytecode.",
        "points": ["lock"],
        "sources": [source.model_dump(mode="json")],
        "hypothetical_example": False,
        "example_text": None,
    }
    evaluation = _evaluation(
        db,
        answer,
        score=1,
        evidence_quotes=["global lock"],
        gap_explanation="Missing threads.",
        reference_answer=reference,
    )
    db.commit()
    evaluation_id = evaluation.id
    db.expunge_all()

    loaded = db.get(Evaluation, evaluation_id)
    assert loaded is not None
    assert loaded.evidence_quotes == ["global lock"]
    assert loaded.gap_explanation == "Missing threads."
    assert loaded.reference_answer == reference
    assert loaded.reference_answer is not None
    assert [SourceRef.model_validate(raw) for raw in loaded.reference_answer["sources"]] == [source]


@pytest.mark.parametrize("score", [0, 4])
def test_evaluation_accepts_score_bounds(db: Session, score: int) -> None:
    session = _session(db)
    answer = _answer(db, session, _question(db, session))
    assert _evaluation(db, answer, score=score).score == score


@pytest.mark.parametrize("score", [-1, 5])
def test_evaluation_score_out_of_range_is_rejected(db: Session, score: int) -> None:
    session = _session(db)
    answer = _answer(db, session, _question(db, session))
    with pytest.raises(IntegrityError):
        _evaluation(db, answer, score=score)


def test_second_evaluation_for_same_answer_is_rejected(db: Session) -> None:
    session = _session(db)
    answer = _answer(db, session, _question(db, session))
    _evaluation(db, answer)
    with pytest.raises(IntegrityError):
        _evaluation(db, answer)


# --- reports ---


def test_report_is_persisted(db: Session) -> None:
    session = _session(db)
    report = _report(db, session)
    db.commit()
    report_id, session_id = report.id, session.id
    db.expunge_all()

    loaded = db.get(Report, report_id)
    assert loaded is not None
    assert loaded.session_id == session_id
    assert loaded.content == {"summary": "Good", "adherence_percentage": "62.5"}
    assert loaded.adherence_percentage == Decimal("62.5")
    assert loaded.model_version == "model-v1"
    assert loaded.rubric_version == "rubric-v1"
    assert loaded.created_at.tzinfo is not None


def test_second_report_for_same_session_is_rejected(db: Session) -> None:
    session = _session(db)
    _report(db, session)
    with pytest.raises(IntegrityError):
        _report(db, session)


def test_eval_12_update_on_report_is_rejected_by_database(db: Session) -> None:
    session = _session(db)
    report = _report(db, session)
    with pytest.raises(DBAPIError, match="reports_immutable"):
        db.execute(update(Report).where(Report.id == report.id).values(model_version="model-v2"))


# --- cascades ---


def test_deleting_session_cascades_answers_evaluations_and_reports(db: Session) -> None:
    session = _session(db)
    answer = _answer(db, session, _question(db, session))
    _evaluation(db, answer)
    _report(db, session)
    session_id = session.id
    answer_id = answer.id

    db.delete(session)
    db.flush()
    db.expunge_all()

    assert db.scalar(select(func.count()).where(Answer.session_id == session_id)) == 0
    assert db.scalar(select(func.count()).where(Evaluation.answer_id == answer_id)) == 0
    assert db.scalar(select(func.count()).where(Report.session_id == session_id)) == 0


# --- migration chain ---


def test_migration_0006_downgrade_and_upgrade(
    migrated_database: str, alembic_config: Config
) -> None:
    head = ScriptDirectory.from_config(alembic_config).get_current_head()
    engine = create_engine(migrated_database)
    try:
        downgrade(alembic_config, "0005")
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == "0005"
            assert not ASSESSMENT_TABLES & set(inspect(connection).get_table_names())
            functions = connection.execute(
                text(
                    "SELECT count(*) FROM pg_proc WHERE proname IN "
                    "('answers_immutable', 'reports_immutable')"
                )
            ).scalar_one()
            assert functions == 0
    finally:
        upgrade(alembic_config, "head")

    try:
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == head
            assert ASSESSMENT_TABLES <= set(inspect(connection).get_table_names())
    finally:
        engine.dispose()
