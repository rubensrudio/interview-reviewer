import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.model_validation.dataset import (
    REQUIRED_CATEGORIES,
    DatasetFormatError,
    ValidationDataset,
    dataset_problems,
    load_dataset,
)

PROMPT_EXAMPLES = [
    "Example answer: I would use a hash map to get O(1) lookups.",
    "Example CV: Jane Doe, backend engineer, 5 years of Go.",
]


def _case(case_id: str, category: str, **overrides: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": case_id,
        "category": category,
        "provenance": "synthetic",
        "kind": "evaluation",
        "input": {"question": f"Synthetic question {case_id}", "answer": f"Answer {case_id}"},
        "expected": {"score": 3},
    }
    case.update(overrides)
    return case


def _complete_cases() -> list[dict[str, Any]]:
    cases = [_case(f"c-{category}", category) for category in REQUIRED_CATEGORIES]
    cases.append(_case("c-verbose-a", "verbosity_pairs", pair_id="p1"))
    cases.append(_case("c-verbose-b", "verbosity_pairs", pair_id="p1", provenance="public"))
    return cases


def _write_dataset(
    root: Path,
    cases: list[dict[str, Any]],
    manifest: dict[str, Any] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if manifest is None:
        manifest = {
            "version": "v1",
            "created_at": "2026-09-30",
            "categories": list(REQUIRED_CATEGORIES),
        }
    (root / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    lines = [json.dumps(case) for case in cases]
    (root / "cases.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def test_model_01_complete_fixture_dataset_has_no_problems(tmp_path: Path) -> None:
    ds = load_dataset(_write_dataset(tmp_path / "v1", _complete_cases()))

    assert isinstance(ds, ValidationDataset)
    assert ds.version == "v1"
    assert len(ds.cases) == len(REQUIRED_CATEGORIES) + 2
    assert dataset_problems(ds, PROMPT_EXAMPLES) == []


def test_model_01_missing_injection_category_is_reported(tmp_path: Path) -> None:
    cases = [c for c in _complete_cases() if c["category"] != "injection"]
    ds = load_dataset(_write_dataset(tmp_path / "v1", cases))

    assert "missing category: injection" in dataset_problems(ds, PROMPT_EXAMPLES)


@pytest.mark.parametrize("category", REQUIRED_CATEGORIES)
def test_model_01_every_required_category_is_checked(tmp_path: Path, category: str) -> None:
    cases = [c for c in _complete_cases() if c["category"] != category]
    ds = load_dataset(_write_dataset(tmp_path / "v1", cases))

    assert dataset_problems(ds, []) == [f"missing category: {category}"]


def test_data_09_user_provenance_is_reported(tmp_path: Path) -> None:
    cases = _complete_cases()
    cases[0]["provenance"] = "user"
    ds = load_dataset(_write_dataset(tmp_path / "v1", cases))

    problems = dataset_problems(ds, PROMPT_EXAMPLES)

    assert problems == [f"invalid provenance in case {cases[0]['id']}: user"]


def test_data_09_missing_provenance_is_reported(tmp_path: Path) -> None:
    cases = _complete_cases()
    del cases[1]["provenance"]
    ds = load_dataset(_write_dataset(tmp_path / "v1", cases))

    problems = dataset_problems(ds, PROMPT_EXAMPLES)

    assert problems == [f"invalid provenance in case {cases[1]['id']}: missing"]


def test_model_01_case_identical_to_prompt_example_is_reported(tmp_path: Path) -> None:
    cases = _complete_cases()
    cases[2]["input"] = {"question": "Q?", "answer": PROMPT_EXAMPLES[0]}
    ds = load_dataset(_write_dataset(tmp_path / "v1", cases))

    problems = dataset_problems(ds, PROMPT_EXAMPLES)

    assert problems == [f"case {cases[2]['id']} duplicates a prompt example"]


def test_model_01_prompt_example_match_ignores_case_and_whitespace(tmp_path: Path) -> None:
    cases = _complete_cases()
    cases[3]["input"] = "  EXAMPLE CV:   Jane Doe, backend engineer,\n5 years of Go. "
    ds = load_dataset(_write_dataset(tmp_path / "v1", cases))

    problems = dataset_problems(ds, PROMPT_EXAMPLES)

    assert problems == [f"case {cases[3]['id']} duplicates a prompt example"]


def test_model_01_manifest_without_version_is_reported(tmp_path: Path) -> None:
    manifest = {"created_at": "2026-09-30", "categories": list(REQUIRED_CATEGORIES)}
    ds = load_dataset(_write_dataset(tmp_path / "v1", _complete_cases(), manifest))

    assert ds.version is None
    assert dataset_problems(ds, PROMPT_EXAMPLES) == ["manifest missing version"]


def test_load_dataset_rejects_missing_files(tmp_path: Path) -> None:
    with pytest.raises(DatasetFormatError):
        load_dataset(tmp_path / "absent")


def test_load_dataset_rejects_invalid_jsonl_line(tmp_path: Path) -> None:
    root = _write_dataset(tmp_path / "v1", _complete_cases())
    with (root / "cases.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")

    with pytest.raises(DatasetFormatError, match="line"):
        load_dataset(root)


def test_load_dataset_rejects_non_mapping_manifest(tmp_path: Path) -> None:
    root = _write_dataset(tmp_path / "v1", _complete_cases())
    (root / "manifest.yaml").write_text("- just\n- a list\n", encoding="utf-8")

    with pytest.raises(DatasetFormatError, match="manifest"):
        load_dataset(root)


def test_load_dataset_skips_blank_lines(tmp_path: Path) -> None:
    root = _write_dataset(tmp_path / "v1", _complete_cases())
    content = (root / "cases.jsonl").read_text(encoding="utf-8")
    (root / "cases.jsonl").write_text("\n" + content + "\n\n", encoding="utf-8")

    assert len(load_dataset(root).cases) == len(REQUIRED_CATEGORIES) + 2
