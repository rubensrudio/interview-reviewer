"""Side-by-side comparison of two completed reports (CT-50; CMP-01, CMP-02).

`compare_reports` only reads what was frozen when each session completed: the stored
percentage, model and rubric versions and the `reports.content` JSON. It never recalculates a
score or an average and never writes anything (EVAL-12).

Skills are matched by normalized name (Unicode NFKC, case folding, collapsed whitespace). Two
reports are not directly comparable when their confirmed requirements, their questions, their
model version or their rubric version differ (CMP-02). Content that cannot be read is treated
as different, so the warning is shown rather than hidden.

This module never logs: report content is personal data.
"""

import unicodedata
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.models.assessment import Report

__all__ = [
    "DIFF_MODEL_VERSION",
    "DIFF_QUESTIONS",
    "DIFF_REQUIREMENTS",
    "DIFF_RUBRIC_VERSION",
    "Comparison",
    "SkillPair",
    "compare_reports",
]

# Labels of `Comparison.differences`, always listed in this order.
DIFF_REQUIREMENTS = "requirements"
DIFF_QUESTIONS = "questions"
DIFF_MODEL_VERSION = "model_version"
DIFF_RUBRIC_VERSION = "rubric_version"

_ONE_PLACE = Decimal("0.1")

# Requirement groups of the frozen content, compared group by group.
_Requirements = tuple[frozenset[str], frozenset[str], frozenset[str]]


@dataclass(frozen=True)
class SkillPair:
    """A skill present in both reports with each stored average (decimal strings)."""

    skill: str
    a_average: str
    b_average: str


@dataclass(frozen=True)
class Comparison:
    """Result of `compare_reports`; percentages are decimal strings with one place."""

    a_percentage: str
    b_percentage: str
    common_skills: list[SkillPair] = field(default_factory=list)
    comparable: bool = True
    differences: list[str] = field(default_factory=list)


def compare_reports(a: Report, b: Report) -> Comparison:
    """Compare two stored reports without recalculating them."""
    a_content = a.content if isinstance(a.content, dict) else {}
    b_content = b.content if isinstance(b.content, dict) else {}

    differences: list[str] = []
    if _differs(_requirements(a_content), _requirements(b_content)):
        differences.append(DIFF_REQUIREMENTS)
    if _differs(_questions(a_content), _questions(b_content)):
        differences.append(DIFF_QUESTIONS)
    if a.model_version != b.model_version:
        differences.append(DIFF_MODEL_VERSION)
    if a.rubric_version != b.rubric_version:
        differences.append(DIFF_RUBRIC_VERSION)

    return Comparison(
        a_percentage=_one_place(a.adherence_percentage),
        b_percentage=_one_place(b.adherence_percentage),
        common_skills=_common_skills(a_content, b_content),
        comparable=not differences,
        differences=differences,
    )


def _normalize_name(name: str) -> str:
    """Normalized form used to match skill and requirement names."""
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def _differs(a: object | None, b: object | None) -> bool:
    # Unreadable content on either side cannot be shown to match (CMP-02, safe side).
    return a is None or b is None or a != b


def _one_place(value: Decimal) -> str:
    return str(Decimal(value).quantize(_ONE_PLACE, rounding=ROUND_HALF_UP))


def _name_set(raw: Any) -> frozenset[str] | None:
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        return None
    return frozenset(_normalize_name(name) for name in raw)


def _requirements(content: dict[str, Any]) -> _Requirements | None:
    plan = content.get("plan")
    non_evaluated = content.get("non_evaluated")
    if not isinstance(plan, dict) or not isinstance(non_evaluated, dict):
        return None
    mandatory = _name_set(plan.get("skills"))
    nice_to_have = _name_set(non_evaluated.get("nice_to_have"))
    non_technical = _name_set(non_evaluated.get("non_technical"))
    if mandatory is None or nice_to_have is None or non_technical is None:
        return None
    return mandatory, nice_to_have, non_technical


def _questions(content: dict[str, Any]) -> list[tuple[int, str]] | None:
    items = content.get("items")
    if not isinstance(items, list):
        return None
    questions: list[tuple[int, str]] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        position = item.get("position")
        text = item.get("question")
        if not isinstance(position, int) or isinstance(position, bool):
            return None
        if not isinstance(text, str):
            return None
        questions.append((position, " ".join(text.split())))
    return sorted(questions)


def _skill_averages(content: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Map normalized skill name to (display name, stored average); bad entries are skipped."""
    skills = content.get("skills")
    if not isinstance(skills, list):
        return {}
    averages: dict[str, tuple[str, str]] = {}
    for entry in skills:
        if not isinstance(entry, dict):
            continue
        name = entry.get("skill")
        average = entry.get("average")
        if not isinstance(name, str) or not isinstance(average, str):
            continue
        key = _normalize_name(name)
        if key:
            averages.setdefault(key, (name.strip(), average))
    return averages


def _common_skills(a_content: dict[str, Any], b_content: dict[str, Any]) -> list[SkillPair]:
    b_averages = _skill_averages(b_content)
    pairs: list[SkillPair] = []
    for key, (name, a_average) in _skill_averages(a_content).items():
        match = b_averages.get(key)
        if match is not None:
            pairs.append(SkillPair(skill=name, a_average=a_average, b_average=match[1]))
    return pairs
