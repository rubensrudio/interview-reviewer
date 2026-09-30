import pytest

from app.model_validation.metrics import (
    ScoreAgreement,
    extraction_f1,
    injection_success_rate,
    score_agreement,
    structuring_accuracy,
    verbosity_bias_rate,
)


def test_model_05_verbosity_bias_rate_counts_longer_with_higher_score() -> None:
    assert verbosity_bias_rate([(2, 3), (3, 3)]) == 0.5


def test_model_05_verbosity_bias_rate_ignores_longer_with_lower_score() -> None:
    assert verbosity_bias_rate([(3, 2), (1, 2)]) == 0.5


def test_model_02_extraction_f1_partial_recall() -> None:
    assert round(extraction_f1(["python", "sql"], ["python"]), 3) == 0.667


def test_model_02_extraction_f1_is_case_and_space_insensitive_and_deduplicated() -> None:
    assert extraction_f1(["Python", "SQL"], [" python", "sql", "sql"]) == 1.0


def test_model_02_extraction_f1_no_overlap_is_zero() -> None:
    assert extraction_f1(["python"], ["java"]) == 0.0


def test_model_02_extraction_f1_nothing_extracted_is_zero() -> None:
    assert extraction_f1(["python"], []) == 0.0


def test_model_02_score_agreement() -> None:
    assert score_agreement([(3, 3), (2, 4)]) == ScoreAgreement(
        exact=0.5, within_one=0.5, mae=1.0
    )


def test_model_02_score_agreement_within_one_includes_adjacent() -> None:
    result = score_agreement([(3, 2), (0, 1)])
    assert result.exact == 0.0
    assert result.within_one == 1.0
    assert result.mae == 1.0


def test_model_02_structuring_accuracy_is_fraction_of_matching_cases() -> None:
    cases = [
        ({"skills": ["python"]}, {"skills": ["python"]}),
        ({"skills": ["sql"]}, {"skills": ["java"]}),
    ]
    assert structuring_accuracy(cases) == 0.5


def test_model_90_injection_success_rate() -> None:
    assert injection_success_rate([True, False, False, False]) == 0.25


@pytest.mark.parametrize(
    "call",
    [
        lambda: extraction_f1([], []),
        lambda: structuring_accuracy([]),
        lambda: score_agreement([]),
        lambda: verbosity_bias_rate([]),
        lambda: injection_success_rate([]),
    ],
)
def test_model_02_empty_inputs_raise_value_error(call) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        call()
