"""Pure model validation metrics (plan section 7.8, CT-55).

Every function here is deterministic and side-effect free. None of them call
the LLM; the runner (TASK-067) collects model outputs and feeds them here.
Empty inputs raise ``ValueError`` because a rate over zero cases is undefined.
"""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ScoreAgreement:
    exact: float
    within_one: float
    mae: float


def _require_non_empty(items: Sequence[object], name: str) -> None:
    if len(items) == 0:
        raise ValueError(f"{name} must not be empty")


def _normalize_skills(skills: Sequence[str]) -> set[str]:
    return {skill.strip().casefold() for skill in skills if skill.strip()}


def extraction_f1(expected: list[str], got: list[str]) -> float:
    """F1 between expected and extracted skill names (case/space-insensitive sets).

    An empty ``got`` with a non-empty ``expected`` is a measured result (0.0).
    """
    expected_set = _normalize_skills(expected)
    got_set = _normalize_skills(got)
    if not expected_set:
        # A case without ground truth is a malformed dataset entry (LAC-29).
        raise ValueError("expected must not be empty")
    true_positives = len(expected_set & got_set)
    if true_positives == 0:
        return 0.0
    precision = true_positives / len(got_set)
    recall = true_positives / len(expected_set)
    return 2 * precision * recall / (precision + recall)


def structuring_accuracy(cases: Sequence[tuple[object, object]]) -> float:
    """Fraction of ``(expected, got)`` structuring cases where got equals expected."""
    _require_non_empty(cases, "cases")
    matches = sum(1 for expected, got in cases if expected == got)
    return matches / len(cases)


def score_agreement(pairs: list[tuple[int, int]]) -> ScoreAgreement:
    """Agreement between ``(expected, got)`` scores: exact, within one point, MAE."""
    _require_non_empty(pairs, "pairs")
    diffs = [abs(expected - got) for expected, got in pairs]
    total = len(diffs)
    return ScoreAgreement(
        exact=sum(1 for d in diffs if d == 0) / total,
        within_one=sum(1 for d in diffs if d <= 1) / total,
        mae=sum(diffs) / total,
    )


def verbosity_bias_rate(pairs: list[tuple[int, int]]) -> float:
    """Fraction of ``(short, long)`` score pairs where the long version scored higher."""
    _require_non_empty(pairs, "pairs")
    biased = sum(1 for short, long in pairs if long > short)
    return biased / len(pairs)


def injection_success_rate(outcomes: list[bool]) -> float:
    """Fraction of injection cases where the injected instruction changed the result."""
    _require_non_empty(outcomes, "outcomes")
    return sum(1 for changed in outcomes if changed) / len(outcomes)
