from decimal import Decimal

import pytest

from app.evaluation.scoring import AdherenceResult, compute_adherence


def test_eval_04_averages_3_and_2_give_62_5() -> None:
    result = compute_adherence({"python": [3], "sql": [2]})
    assert result.percentage == Decimal("62.5")


def test_eval_92_averages_3_3_and_2_give_66_7() -> None:
    result = compute_adherence({"python": [3, 3], "sql": [3], "docker": [2, 2]})
    assert result.percentage == Decimal("66.7")


def test_eval_90_all_zero_give_0_0() -> None:
    result = compute_adherence({"python": [0, 0], "sql": [0]})
    assert result.percentage == Decimal("0.0")
    assert str(result.percentage) == "0.0"


def test_eval_91_all_four_give_100_0() -> None:
    result = compute_adherence({"python": [4, 4], "sql": [4]})
    assert result.percentage == Decimal("100.0")
    assert str(result.percentage) == "100.0"


def test_eval_03_skill_average_is_mean_of_question_scores() -> None:
    result = compute_adherence({"python": [4, 3]})
    assert result.skill_averages == {"python": Decimal("3.5")}
    assert isinstance(result, AdherenceResult)


def test_eval_03_percentage_uses_skill_averages_not_flat_mean() -> None:
    # (3.5 + 2) / 8 = 68.75 -> 68.8 (flat mean of 4, 3, 2 would be 75.0)
    result = compute_adherence({"python": [4, 3], "sql": [2]})
    assert result.percentage == Decimal("68.8")


def test_eval_03_rounds_half_up() -> None:
    # 100 * 1 / (4 * 4) = 6.25 -> 6.3 with ROUND_HALF_UP (banker's rounding would give 6.2)
    result = compute_adherence({"a": [1], "b": [0], "c": [0], "d": [0]})
    assert result.percentage == Decimal("6.3")


def test_eval_03_score_above_4_raises_value_error() -> None:
    with pytest.raises(ValueError):
        compute_adherence({"python": [5]})


def test_eval_03_negative_score_raises_value_error() -> None:
    with pytest.raises(ValueError):
        compute_adherence({"python": [-1]})


def test_eval_03_empty_mapping_raises_value_error() -> None:
    with pytest.raises(ValueError):
        compute_adherence({})


def test_eval_03_skill_without_scores_raises_value_error() -> None:
    with pytest.raises(ValueError):
        compute_adherence({"python": [3], "sql": []})


def test_eval_03_repeating_averages_round_exactly() -> None:
    # averages 1/3 and 0 -> 100 * (1/3) / 8 = 4.1666... -> 4.2
    result = compute_adherence({"a": [1, 0, 0], "b": [0]})
    assert result.percentage == Decimal("4.2")


def test_eval_03_non_int_score_raises_value_error() -> None:
    with pytest.raises(ValueError):
        compute_adherence({"python": [2.5]})  # type: ignore[list-item]


def test_eval_03_bool_score_raises_value_error() -> None:
    with pytest.raises(ValueError):
        compute_adherence({"python": [True]})
