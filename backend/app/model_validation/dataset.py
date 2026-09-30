"""Validation dataset loader and checker (plan section 7.8, CT-54).

A dataset lives in a directory holding ``manifest.yaml`` and ``cases.jsonl``.
Only synthetic or public material is allowed (DATA-09); nothing here reads
from the production database.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MANIFEST_FILE = "manifest.yaml"
CASES_FILE = "cases.jsonl"

REQUIRED_CATEGORIES: tuple[str, ...] = (
    "varied_pdfs",
    "missing_skills",
    "synonyms",
    "correct_rephrased",
    "wrong_answers",
    "verbosity_pairs",
    "injection",
)
ALLOWED_PROVENANCE: frozenset[str] = frozenset({"synthetic", "public"})

_WHITESPACE_RE = re.compile(r"\s+")


class DatasetFormatError(ValueError):
    """Raised when the dataset files are missing or cannot be parsed."""


@dataclass(frozen=True)
class ValidationCase:
    id: str | None
    category: str | None
    provenance: str | None
    kind: str | None
    input: Any
    expected: Any
    pair_id: str | None = None


@dataclass(frozen=True)
class ValidationDataset:
    path: Path
    version: str | None
    created_at: str | None
    categories: list[str] = field(default_factory=list)
    cases: list[ValidationCase] = field(default_factory=list)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DatasetFormatError(f"cannot read manifest: {path.name}") from exc
    except yaml.YAMLError as exc:
        raise DatasetFormatError(f"invalid YAML in manifest: {path.name}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DatasetFormatError("manifest must be a mapping")
    return raw


def _load_cases(path: Path) -> list[ValidationCase]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DatasetFormatError(f"cannot read cases file: {path.name}") from exc
    cases: list[ValidationCase] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetFormatError(f"invalid JSON in {path.name} at line {number}") from exc
        if not isinstance(raw, dict):
            raise DatasetFormatError(f"case in {path.name} at line {number} must be an object")
        cases.append(
            ValidationCase(
                id=_optional_str(raw.get("id")),
                category=_optional_str(raw.get("category")),
                provenance=_optional_str(raw.get("provenance")),
                kind=_optional_str(raw.get("kind")),
                input=raw.get("input"),
                expected=raw.get("expected"),
                pair_id=_optional_str(raw.get("pair_id")),
            )
        )
    return cases


def load_dataset(path: Path) -> ValidationDataset:
    """Load a dataset directory. Content problems are left to ``dataset_problems``."""
    manifest = _load_manifest(path / MANIFEST_FILE)
    raw_categories = manifest.get("categories") or []
    if not isinstance(raw_categories, list):
        raise DatasetFormatError("manifest categories must be a list")
    version = manifest.get("version")
    return ValidationDataset(
        path=path,
        version=None if version in (None, "") else str(version),
        created_at=_optional_str(manifest.get("created_at")),
        categories=[str(category) for category in raw_categories],
        cases=_load_cases(path / CASES_FILE),
    )


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip().casefold()


def _text_leaves(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _text_leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _text_leaves(item)]
    return []


def dataset_problems(ds: ValidationDataset, prompt_examples: list[str]) -> list[str]:
    """Return human-readable problems; an empty list means the dataset is usable."""
    problems: list[str] = []
    if ds.version is None:
        problems.append("manifest missing version")

    present = {case.category for case in ds.cases}
    problems.extend(
        f"missing category: {category}"
        for category in REQUIRED_CATEGORIES
        if category not in present
    )

    examples = {_normalize(example) for example in prompt_examples if example.strip()}
    for index, case in enumerate(ds.cases, start=1):
        label = case.id if case.id is not None else f"#{index}"
        if case.provenance not in ALLOWED_PROVENANCE:
            shown = case.provenance if case.provenance is not None else "missing"
            problems.append(f"invalid provenance in case {label}: {shown}")
        if any(_normalize(text) in examples for text in _text_leaves(case.input)):
            problems.append(f"case {label} duplicates a prompt example")
    return problems
