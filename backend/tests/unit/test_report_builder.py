import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.evaluation.scoring import compute_adherence
from app.llm.model_version import ModelVersion
from app.models.assessment import Answer, Evaluation
from app.models.interview import InterviewSession, Question, SessionStatus
from app.reports.builder import REPORT_DISCLAIMER, ReportContent, build_report_content

MODEL_VERSION = ModelVersion(base_model="qwen", config_version="v1", id="qwen+v1")
RUBRIC_VERSION = "rubric-1"
COMPLETED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

PY_SOURCE: dict[str, Any] = {
    "url": "https://docs.python.org/3/tutorial/",
    "title": "Python Tutorial",
    "collected_at": "2026-08-01T10:00:00+00:00",
    "excerpt": "Python is an easy to learn, powerful programming language.",
}
SQL_SOURCE: dict[str, Any] = {
    "url": "https://www.postgresql.org/docs/current/tutorial-join.html",
    "title": "Joins Between Tables",
    "collected_at": "2026-08-02T10:00:00+00:00",
    "excerpt": "Queries can access multiple tables at once.",
}


def _session(**overrides: Any) -> InterviewSession:
    session = InterviewSession(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status=SessionStatus.EVALUATING,
        language="en",
        requirement_items=[
            {
                "id": "r1",
                "name": "Python",
                "original_terms": ["python"],
                "classification": "required",
            },
            {
                "id": "r2",
                "name": "SQL",
                "original_terms": ["sql"],
                "classification": "required",
            },
            {
                "id": "r3",
                "name": "Kubernetes",
                "original_terms": ["k8s"],
                "classification": "nice_to_have",
            },
        ],
        non_technical=["English", "5 years of experience"],
        proposal={"planned_count": 2, "skills": ["Python", "SQL"]},
        planned_count=2,
        answered_count=2,
        completed_at=COMPLETED_AT,
    )
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


def _question(
    session: InterviewSession,
    position: int,
    skill: str,
    sources: list[dict[str, Any]],
    no_verified_source: bool = False,
) -> Question:
    return Question(
        id=uuid.uuid4(),
        session_id=session.id,
        position=position,
        skill_name=skill,
        text=f"Question about {skill}?",
        reference_points=[f"{skill} point"],
        sources=sources,
        no_verified_source=no_verified_source,
    )


def _answer(question: Question, content: str) -> Answer:
    return Answer(
        id=uuid.uuid4(),
        session_id=question.session_id,
        question_id=question.id,
        content=content,
        idempotency_key=uuid.uuid4().hex,
    )


def _evaluation(
    answer: Answer, score: int, reference_answer: dict[str, Any] | None = None
) -> Evaluation:
    return Evaluation(
        id=uuid.uuid4(),
        answer_id=answer.id,
        score=score,
        justification=f"Justification for score {score}.",
        evidence_quotes=[answer.content],
        gap_explanation=None if score >= 3 else "Missing key concepts.",
        reference_answer=reference_answer,
        model_version=MODEL_VERSION.id,
    )


def _reference(sources: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "text": "A complete reference answer.",
        "points": ["point one", "point two"],
        "sources": sources,
        "hypothetical_example": True,
        "example_text": "Hypothetical example text.",
    }


def _build(
    scores: dict[str, int],
    *,
    session: InterviewSession | None = None,
    no_source_skills: frozenset[str] = frozenset(),
) -> ReportContent:
    session = session or _session()
    source_by_skill = {"Python": PY_SOURCE, "SQL": SQL_SOURCE}
    questions: list[Question] = []
    answers: list[Answer] = []
    evaluations: list[Evaluation] = []
    for position, (skill, score) in enumerate(scores.items(), start=1):
        no_source = skill in no_source_skills
        sources = [] if no_source else [dict(source_by_skill[skill])]
        question = _question(session, position, skill, sources, no_verified_source=no_source)
        answer = _answer(question, f"My answer about {skill}.")
        reference = _reference(sources) if score < 3 else None
        questions.append(question)
        answers.append(answer)
        evaluations.append(_evaluation(answer, score, reference))
    adherence = compute_adherence({skill: [score] for skill, score in scores.items()})
    # Shuffle input order: the builder must not depend on it.
    return build_report_content(
        session,
        list(reversed(questions)),
        answers,
        list(reversed(evaluations)),
        adherence,
        MODEL_VERSION,
        RUBRIC_VERSION,
    )


def test_eval_91_all_scores_4_have_no_unsatisfactory_items() -> None:
    report = _build({"Python": 4, "SQL": 4})

    assert report.unsatisfactory_items == []
    assert report.adherence_percentage == "100.0"
    assert all(item.satisfactory for item in report.items)
    assert all(item.reference_answer is None for item in report.items)


def test_eval_04_percentage_and_unsatisfactory_items() -> None:
    report = _build({"Python": 3, "SQL": 2})

    assert report.adherence_percentage == "62.5"
    assert report.unsatisfactory_items == [2]
    sql_item = report.items[1]
    assert sql_item.position == 2
    assert sql_item.satisfactory is False
    assert sql_item.gap_explanation == "Missing key concepts."
    assert sql_item.reference_answer is not None
    assert sql_item.reference_answer.points == ["point one", "point two"]


def test_eval_08_items_are_ordered_and_complete() -> None:
    report = _build({"Python": 4, "SQL": 1})

    assert [item.position for item in report.items] == [1, 2]
    first = report.items[0]
    assert first.skill == "Python"
    assert first.question == "Question about Python?"
    assert first.answer == "My answer about Python."
    assert first.score == 4
    assert first.justification == "Justification for score 4."
    assert first.evidence_quotes == ["My answer about Python."]
    assert report.summary
    assert "2" in report.summary


def test_eval_08_skills_show_average_and_positions() -> None:
    report = _build({"Python": 4, "SQL": 1})

    assert [(s.skill, s.average, s.question_positions) for s in report.skills] == [
        ("Python", "4.0", [1]),
        ("SQL", "1.0", [2]),
    ]


def test_eval_05_nice_to_have_skill_listed_as_not_evaluated() -> None:
    report = _build({"Python": 3, "SQL": 3})

    assert report.non_evaluated.nice_to_have == ["Kubernetes"]
    assert "Kubernetes" not in [skill.skill for skill in report.skills]


def test_plan_06_non_technical_requirements_listed_as_not_evaluated() -> None:
    report = _build({"Python": 3, "SQL": 3})

    assert report.non_evaluated.non_technical == ["English", "5 years of experience"]


def test_eval_15_no_verified_source_item_has_mark_and_no_sources() -> None:
    report = _build({"Python": 1, "SQL": 4}, no_source_skills=frozenset({"Python"}))

    item = report.items[0]
    assert item.no_verified_source is True
    assert item.reference_answer is not None
    assert item.reference_answer.sources == []
    assert PY_SOURCE["url"] not in [source.url for source in report.sources_used]


def test_eval_15_no_verified_source_drops_stray_reference_sources() -> None:
    session = _session()
    question = _question(session, 1, "Python", [], no_verified_source=True)
    answer = _answer(question, "answer")
    evaluation = _evaluation(answer, 0, _reference([PY_SOURCE]))

    report = build_report_content(
        session,
        [question],
        [answer],
        [evaluation],
        compute_adherence({"Python": [0]}),
        MODEL_VERSION,
        RUBRIC_VERSION,
    )

    assert report.items[0].reference_answer is not None
    assert report.items[0].reference_answer.sources == []
    assert report.sources_used == []


def test_know_08_sources_used_are_copied_with_excerpt_title_and_date() -> None:
    report = _build({"Python": 2, "SQL": 4})

    by_url = {source.url: source for source in report.sources_used}
    assert set(by_url) == {PY_SOURCE["url"], SQL_SOURCE["url"]}
    python = by_url[PY_SOURCE["url"]]
    assert python.title == PY_SOURCE["title"]
    assert python.excerpt == PY_SOURCE["excerpt"]
    assert python.collected_at == datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    dumped = report.model_dump(mode="json")
    assert dumped["sources_used"][0].keys() == {"url", "title", "collected_at", "excerpt"}


def test_know_91_sources_used_are_deduplicated_by_url() -> None:
    report = _build({"Python": 1, "SQL": 4})

    urls = [source.url for source in report.sources_used]
    assert len(urls) == len(set(urls))


def test_eval_08_disclaimer_is_exact_spec_text() -> None:
    report = _build({"Python": 3, "SQL": 3})

    expected = (
        "This percentage reflects your answers in this session only. It is not a hiring "
        "prediction or a certification of professional competence."
    )
    assert report.disclaimer == expected
    assert REPORT_DISCLAIMER == expected


def test_eval_11_records_versions_plan_and_completion() -> None:
    report = _build({"Python": 3, "SQL": 3})

    assert report.model_version == "qwen+v1"
    assert report.rubric_version == RUBRIC_VERSION
    assert report.plan.planned_count == 2
    assert report.plan.skills == ["Python", "SQL"]
    assert report.completed_at == COMPLETED_AT


def test_eval_11_missing_completed_at_uses_build_time() -> None:
    before = datetime.now(UTC)
    report = _build({"Python": 3, "SQL": 3}, session=_session(completed_at=None))

    assert report.completed_at >= before


def test_eval_11_plan_falls_back_to_questions_without_proposal() -> None:
    report = _build({"Python": 3, "SQL": 3}, session=_session(proposal=None, planned_count=None))

    assert report.plan.planned_count == 2
    assert report.plan.skills == ["Python", "SQL"]


def test_eval_08_question_without_evaluation_raises() -> None:
    session = _session()
    question = _question(session, 1, "Python", [])
    answer = _answer(question, "answer")

    with pytest.raises(ValueError):
        build_report_content(
            session,
            [question],
            [answer],
            [],
            compute_adherence({"Python": [3]}),
            MODEL_VERSION,
            RUBRIC_VERSION,
        )


def test_report_content_json_round_trip() -> None:
    report = _build({"Python": 1, "SQL": 4})

    restored = ReportContent.model_validate(report.model_dump(mode="json"))
    assert restored == report
