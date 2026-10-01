"""Unit tests for `compare_reports` (TASK-060, CT-50; CMP-01, CMP-02)."""

import copy
import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.models.assessment import Report
from app.reports.compare import (
    DIFF_MODEL_VERSION,
    DIFF_QUESTIONS,
    DIFF_REQUIREMENTS,
    DIFF_RUBRIC_VERSION,
    Comparison,
    SkillPair,
    compare_reports,
)

BASE_CONTENT: dict[str, Any] = {
    "adherence_percentage": "62.5",
    "skills": [
        {"skill": "Python", "average": "2.5", "question_positions": [1, 2]},
        {"skill": "SQL", "average": "3.0", "question_positions": [3]},
    ],
    "items": [
        {"position": 1, "skill": "Python", "question": "Explain the GIL."},
        {"position": 2, "skill": "Python", "question": "What is a generator?"},
        {"position": 3, "skill": "SQL", "question": "What is a LEFT JOIN?"},
    ],
    "non_evaluated": {"nice_to_have": ["Docker"], "non_technical": ["English"]},
    "plan": {"planned_count": 3, "skills": ["Python", "SQL"]},
    "model_version": "qwen+v1",
    "rubric_version": "rubric-1",
}


def _report(
    content: Any = None,
    percentage: str = "62.5",
    model_version: str = "qwen+v1",
    rubric_version: str = "rubric-1",
) -> Report:
    return Report(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        content=copy.deepcopy(BASE_CONTENT) if content is None else content,
        adherence_percentage=Decimal(percentage),
        model_version=model_version,
        rubric_version=rubric_version,
    )


def _content(**changes: Any) -> dict[str, Any]:
    content = copy.deepcopy(BASE_CONTENT)
    content.update(changes)
    return content


def test_cmp02_different_rubric_version_is_not_comparable() -> None:
    a = _report(rubric_version="rubric-1")
    b = _report(rubric_version="rubric-2")

    result = compare_reports(a, b)

    assert result.comparable is False
    assert DIFF_RUBRIC_VERSION in result.differences
    assert "rubric_version" in result.differences


def test_cmp02_different_model_version_is_not_comparable() -> None:
    result = compare_reports(_report(model_version="m-1"), _report(model_version="m-2"))

    assert result.comparable is False
    assert result.differences == [DIFF_MODEL_VERSION]


def test_cmp02_identical_setup_is_comparable_without_differences() -> None:
    result = compare_reports(_report(percentage="50.0"), _report(percentage="75.0"))

    assert result.comparable is True
    assert result.differences == []


def test_cmp02_different_confirmed_requirements_are_not_comparable() -> None:
    other = _content(plan={"planned_count": 3, "skills": ["Python", "Go"]})

    result = compare_reports(_report(), _report(content=other))

    assert result.comparable is False
    assert result.differences == [DIFF_REQUIREMENTS]


def test_cmp02_different_non_evaluated_requirements_are_not_comparable() -> None:
    other = _content(non_evaluated={"nice_to_have": [], "non_technical": ["English"]})

    result = compare_reports(_report(), _report(content=other))

    assert result.differences == [DIFF_REQUIREMENTS]


def test_cmp02_requirements_compare_by_normalized_name_and_ignore_order() -> None:
    other = _content(plan={"planned_count": 3, "skills": [" sql ", "PYTHON"]})

    result = compare_reports(_report(), _report(content=other))

    assert result.comparable is True


def test_cmp02_different_questions_are_not_comparable() -> None:
    other = copy.deepcopy(BASE_CONTENT)
    other["items"][1]["question"] = "What is a decorator?"

    result = compare_reports(_report(), _report(content=other))

    assert result.comparable is False
    assert result.differences == [DIFF_QUESTIONS]


def test_cmp02_all_differences_are_listed_in_fixed_order() -> None:
    other = _content(
        plan={"planned_count": 1, "skills": ["Go"]},
        items=[{"position": 1, "skill": "Go", "question": "What is a goroutine?"}],
    )
    b = _report(content=other, model_version="m-2", rubric_version="rubric-2")

    result = compare_reports(_report(), b)

    assert result.differences == [
        DIFF_REQUIREMENTS,
        DIFF_QUESTIONS,
        DIFF_MODEL_VERSION,
        DIFF_RUBRIC_VERSION,
    ]


@pytest.mark.parametrize("broken", [{}, [], "text", {"items": "x", "plan": 3, "skills": None}])
def test_cmp02_unreadable_content_is_flagged_as_not_comparable(broken: Any) -> None:
    result = compare_reports(_report(), _report(content=broken))

    assert result.comparable is False
    assert DIFF_REQUIREMENTS in result.differences
    assert DIFF_QUESTIONS in result.differences
    assert result.common_skills == []


def test_cmp01_common_skills_are_listed_with_both_averages() -> None:
    other = _content(
        skills=[
            {"skill": "sql", "average": "3.5", "question_positions": [1]},
            {"skill": "Go", "average": "1.0", "question_positions": [2]},
            {"skill": " python ", "average": "4.0", "question_positions": [3]},
        ]
    )

    result = compare_reports(_report(), _report(content=other))

    assert result.common_skills == [
        SkillPair(skill="Python", a_average="2.5", b_average="4.0"),
        SkillPair(skill="SQL", a_average="3.0", b_average="3.5"),
    ]


def test_cmp01_percentages_are_the_stored_values_as_strings() -> None:
    result = compare_reports(_report(percentage="62.5"), _report(percentage="80"))

    assert isinstance(result, Comparison)
    assert result.a_percentage == "62.5"
    assert result.b_percentage == "80.0"


def test_cmp01_skill_entries_with_invalid_shape_are_ignored() -> None:
    other = _content(
        skills=[
            {"skill": "Python", "average": 2.5},
            {"skill": None, "average": "1.0"},
            "SQL",
            {"skill": "SQL", "average": "2.0"},
        ]
    )

    result = compare_reports(_report(), _report(content=other))

    assert result.common_skills == [SkillPair(skill="SQL", a_average="3.0", b_average="2.0")]


def test_cmp01_compare_does_not_modify_reports() -> None:
    a = _report()
    b = _report(rubric_version="rubric-2")
    before = (copy.deepcopy(a.content), copy.deepcopy(b.content))

    compare_reports(a, b)

    assert (a.content, b.content) == before
    assert a.adherence_percentage == Decimal("62.5")
