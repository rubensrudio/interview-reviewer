"""Deterministic adherence scoring (CT-44).

Only mandatory skills are passed in; desirable skills never affect the
percentage. No float arithmetic is used: averages and the percentage are
computed exactly with ``Fraction`` and converted to ``Decimal`` at the end.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

MIN_SCORE = 0
MAX_SCORE = 4
_PERCENT_QUANTUM = Decimal("0.1")


@dataclass(frozen=True)
class AdherenceResult:
    skill_averages: dict[str, Decimal]
    percentage: Decimal


def _validate_scores(skill: str, scores: Sequence[int]) -> None:
    if len(scores) == 0:
        raise ValueError(f"skill {skill!r} has no scores")
    for score in scores:
        if isinstance(score, bool) or not isinstance(score, int):
            raise ValueError(f"skill {skill!r} has a non-integer score")
        if not MIN_SCORE <= score <= MAX_SCORE:
            raise ValueError(f"skill {skill!r} has a score outside {MIN_SCORE}-{MAX_SCORE}")


def _fraction_to_decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


def _round_half_up_one_place(value: Fraction) -> Decimal:
    # Exact integer rounding avoids any precision loss from inexact Decimal
    # division on repeating fractions; the result is then normalised to one
    # decimal place with ROUND_HALF_UP semantics (value is never negative).
    tenths = value * 10
    rounded = (2 * tenths.numerator + tenths.denominator) // (2 * tenths.denominator)
    return (Decimal(rounded) / 10).quantize(_PERCENT_QUANTUM, rounding=ROUND_HALF_UP)


def compute_adherence(scores_by_skill: Mapping[str, Sequence[int]]) -> AdherenceResult:
    """Return per-skill averages and the adherence percentage.

    percentage = 100 * sum(skill averages) / (4 * M), rounded half-up to one
    decimal place, where M is the number of mandatory skills.
    """
    if len(scores_by_skill) == 0:
        raise ValueError("at least one mandatory skill is required")

    exact_averages: dict[str, Fraction] = {}
    for skill, scores in scores_by_skill.items():
        _validate_scores(skill, scores)
        exact_averages[skill] = Fraction(sum(scores), len(scores))

    total = sum(exact_averages.values(), Fraction(0))
    percentage = total * 100 / (MAX_SCORE * len(exact_averages))

    return AdherenceResult(
        skill_averages={skill: _fraction_to_decimal(avg) for skill, avg in exact_averages.items()},
        percentage=_round_half_up_one_place(percentage),
    )
