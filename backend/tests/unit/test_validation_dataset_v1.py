"""Checks on the committed validation dataset v1 (MODEL-01, DATA-09, MODEL-90)."""

import re
from collections import Counter, defaultdict
from pathlib import Path

import pytest
import yaml

from app.model_validation.dataset import (
    ALLOWED_PROVENANCE,
    REQUIRED_CATEGORIES,
    ValidationCase,
    ValidationDataset,
    dataset_problems,
    load_dataset,
)

DATASET_DIR = Path(__file__).resolve().parents[2] / "validation" / "v1"
MIN_CASES_PER_CATEGORY = 5
ALLOWED_KINDS = frozenset({"extraction", "structuring", "evaluation"})

# Stand-in for the few-shot examples embedded in model instructions. The dataset must never
# reuse them verbatim (MODEL-01); the runner (TASK-067) passes the real prompt examples.
PROMPT_EXAMPLES = [
    "Example answer: I would use a hash map to get O(1) lookups.",
    "Example CV: Jane Doe, backend engineer, 5 years of Go.",
]

# Personal data markers that must never appear in a synthetic dataset (DATA-09).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\(?\d{2,4}\)?[\s.-]?\d{3,5}[\s.-]?\d{4}\b")
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)


@pytest.fixture(scope="module")
def dataset() -> ValidationDataset:
    return load_dataset(DATASET_DIR)


def _texts(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _texts(item)]
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    return []


def _by_category(ds: ValidationDataset, category: str) -> list[ValidationCase]:
    return [case for case in ds.cases if case.category == category]


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def test_model_01_dataset_v1_has_no_problems(dataset: ValidationDataset) -> None:
    assert dataset_problems(dataset, PROMPT_EXAMPLES) == []


def test_model_01_manifest_is_versioned_and_lists_all_categories(
    dataset: ValidationDataset,
) -> None:
    manifest = yaml.safe_load((DATASET_DIR / "manifest.yaml").read_text(encoding="utf-8"))

    assert dataset.version == "v1"
    assert dataset.created_at
    assert sorted(dataset.categories) == sorted(REQUIRED_CATEGORIES)
    assert manifest["version"] == "v1"


@pytest.mark.parametrize("category", REQUIRED_CATEGORIES)
def test_model_01_every_category_has_at_least_five_cases(
    dataset: ValidationDataset, category: str
) -> None:
    assert len(_by_category(dataset, category)) >= MIN_CASES_PER_CATEGORY


def test_model_01_no_case_outside_declared_categories(dataset: ValidationDataset) -> None:
    assert {case.category for case in dataset.cases} <= set(REQUIRED_CATEGORIES)


def test_model_01_case_ids_are_unique_and_kinds_valid(dataset: ValidationDataset) -> None:
    ids = Counter(case.id for case in dataset.cases)

    assert all(case.id for case in dataset.cases)
    assert [case_id for case_id, count in ids.items() if count > 1] == []
    assert {case.kind for case in dataset.cases} <= ALLOWED_KINDS
    assert all(case.input and case.expected is not None for case in dataset.cases)


def test_data_09_every_case_has_synthetic_or_public_provenance(
    dataset: ValidationDataset,
) -> None:
    assert all(case.provenance in ALLOWED_PROVENANCE for case in dataset.cases)


def test_data_09_no_contact_data_in_cases(dataset: ValidationDataset) -> None:
    texts = [text for case in dataset.cases for text in _texts(case.input)]

    assert [t for t in texts if _EMAIL_RE.search(t)] == []
    assert [t for t in texts if _PHONE_RE.search(t)] == []
    assert [t for t in texts if _URL_RE.search(t)] == []


def test_model_01_evaluation_scores_are_in_rubric_range(dataset: ValidationDataset) -> None:
    for case in dataset.cases:
        if case.kind == "evaluation":
            score = _as_dict(case.expected)["score"]
            assert isinstance(score, int)
            assert 0 <= score <= 4


def test_model_01_verbosity_pairs_share_content_and_differ_in_length(
    dataset: ValidationDataset,
) -> None:
    pairs: dict[str, list[ValidationCase]] = defaultdict(list)
    for case in _by_category(dataset, "verbosity_pairs"):
        assert case.pair_id, f"verbosity case {case.id} has no pair_id"
        pairs[case.pair_id].append(case)

    assert len(pairs) >= 3
    for pair_id, members in pairs.items():
        assert len(members) == 2, f"pair {pair_id} must have exactly two cases"
        first, second = (_as_dict(member.input) for member in members)
        assert first["question"] == second["question"]
        assert _as_dict(members[0].expected)["score"] == _as_dict(members[1].expected)["score"]
        lengths = sorted(len(str(item["answer"])) for item in (first, second))
        assert lengths[1] >= 2 * lengths[0], f"pair {pair_id} verbosity gap too small"


def test_model_01_correct_and_wrong_answers_are_scored_apart(
    dataset: ValidationDataset,
) -> None:
    correct = [_as_dict(c.expected)["score"] for c in _by_category(dataset, "correct_rephrased")]
    wrong = [_as_dict(c.expected)["score"] for c in _by_category(dataset, "wrong_answers")]

    assert all(isinstance(score, int) and score >= 3 for score in correct)
    assert all(isinstance(score, int) and score <= 1 for score in wrong)


def test_model_01_varied_pdfs_cover_distinct_layouts(dataset: ValidationDataset) -> None:
    layouts = {_as_dict(c.input)["pdf_layout"] for c in _by_category(dataset, "varied_pdfs")}

    assert len(layouts) >= MIN_CASES_PER_CATEGORY


def test_model_01_missing_skills_declare_skills_absent_from_resume(
    dataset: ValidationDataset,
) -> None:
    for case in _by_category(dataset, "missing_skills"):
        expected = _as_dict(case.expected)
        raw_skills = _as_dict(case.input)["resume_skills"]
        assert isinstance(raw_skills, list)
        resume_skills = {str(skill).casefold() for skill in raw_skills}
        missing = expected["missing_from_resume"]
        assert isinstance(missing, list) and missing
        assert all(str(skill).casefold() not in resume_skills for skill in missing)


def test_model_01_synonym_cases_map_terms_to_canonical_names(
    dataset: ValidationDataset,
) -> None:
    for case in _by_category(dataset, "synonyms"):
        requirements = _as_dict(case.expected)["requirements"]
        assert isinstance(requirements, list) and requirements
        assert any(
            item["name"] not in item["original_terms"] for item in requirements
        ), f"synonym case {case.id} has no term that differs from its canonical name"


def test_model_90_injection_covers_resume_and_answer(dataset: ValidationDataset) -> None:
    locations: Counter[str] = Counter()
    for case in _by_category(dataset, "injection"):
        injection = _as_dict(_as_dict(case.expected)["injection"])
        location = str(injection["location"])
        locations[location] += 1
        assert injection["target"], f"injection case {case.id} has no injected goal"
        if location == "resume":
            assert case.kind == "extraction"
        else:
            assert location == "answer"
            assert case.kind == "evaluation"

    assert locations["resume"] >= 2
    assert locations["answer"] >= 2
