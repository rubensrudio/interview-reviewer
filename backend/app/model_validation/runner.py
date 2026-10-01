"""Model validation runner (MODEL-02, MODEL-03, MODEL-04, MODEL-05, MODEL-90).

Usage (from ``backend/``)::

    uv run python -m app.model_validation.runner --dataset validation/v1

Every case of the dataset is run on the current model version through the production code
paths: resume extraction (CT-26), job requirements structuring (``structure_requirements_text``,
no session database) and answer evaluation (CT-45). The outputs are compared with the expected
values by the pure metrics of CT-55 and written to
``validation_reports/<model_version_id>.json`` (``ValidationReport``, CT-56).

``meets_targets`` is True only when ``approved_targets`` in the model targets file is a valid
mapping and every metric meets its target; a null, missing or malformed file gives False (fail
closed). The runner never enables a model in production: that is the release gate's job
(``assert_model_release_allowed``).

Failure policy: an unreachable inference server aborts the run, because a report built without
the model would be meaningless. Invalid model output is a measured result: an extraction keeps
no skill, a structuring case does not match and an evaluation gets the score farthest from the
expected one. Resume text, answers, prompts and model outputs are never logged.
"""

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.errors import AppError
from app.evaluation.evaluator import EvaluationInput, EvaluationUnavailable, evaluate_answer
from app.evaluation.scoring import MAX_SCORE, MIN_SCORE
from app.interviews.requirements import structure_requirements_text
from app.llm.client import LLMClient, get_llm_client
from app.llm.model_version import (
    BACKEND_DIR,
    MetricTarget,
    ValidationMetrics,
    ValidationReport,
    current_model_version,
)
from app.logging_setup import configure_logging
from app.model_validation.dataset import (
    ValidationCase,
    ValidationDataset,
    dataset_problems,
    load_dataset,
)
from app.model_validation.metrics import (
    extraction_f1,
    injection_success_rate,
    score_agreement,
    structuring_accuracy,
    verbosity_bias_rate,
)
from app.observability import log_event
from app.resumes.extraction import ExtractionFailed, extract_resume_items

__all__ = [
    "PROMPT_EXAMPLES",
    "ValidationRunError",
    "main",
    "meets_targets",
    "run_validation",
    "structure_case",
    "write_report",
]

# Few-shot examples embedded in the model instructions. The current prompts (extraction,
# requirements structuring, evaluation) carry none; keep this list in sync if one is added,
# so the dataset can be checked against it (MODEL-01).
PROMPT_EXAMPLES: list[str] = []

_KINDS = frozenset({"extraction", "structuring", "evaluation"})


class ValidationRunError(Exception):
    """The run cannot produce a trustworthy report (bad dataset, unreachable model, bad id)."""


@dataclass
class _Collected:
    extraction_scores: list[float] = field(default_factory=list)
    structuring_cases: list[tuple[object, object]] = field(default_factory=list)
    score_pairs: list[tuple[int, int]] = field(default_factory=list)
    pair_scores: dict[str, list[tuple[int, int]]] = field(default_factory=lambda: defaultdict(list))
    injection_outcomes: list[bool] = field(default_factory=list)


@dataclass(frozen=True)
class _Extracted:
    skills: list[str]
    experiences: list[str]


def _fold(values: Sequence[str]) -> set[str]:
    return {value.strip().casefold() for value in values if value.strip()}


def _as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationRunError(f"case {label} must have object input and expected values")
    return value


def _str_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationRunError(f"case {label} has a malformed list of strings")
    return list(value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationRunError(f"case {label} has a malformed text field")
    return value


def _injection_target(expected: dict[str, Any], label: str) -> dict[str, Any] | None:
    injection = expected.get("injection")
    if injection is None:
        return None
    return _as_dict(_as_dict(injection, label).get("target"), label)


# Extraction ---------------------------------------------------------------------------------


def _extract(llm: LLMClient, text: str) -> _Extracted:
    try:
        items = extract_resume_items(llm, text)
    except ExtractionFailed as error:
        if error.reason == "llm_unavailable":
            raise ValidationRunError("LLM unavailable during extraction") from error
        return _Extracted(skills=[], experiences=[])
    skills = [item.fields.get("name", "") for item in items if item.kind == "skill"]
    experiences = [
        f"{item.fields.get('title', '')} at {item.fields.get('organization', '')}"
        for item in items
        if item.kind == "experience"
    ]
    return _Extracted(skills=[s for s in skills if s], experiences=experiences)


def _resume_injection_changed(target: dict[str, Any], got: _Extracted, label: str) -> bool:
    got_skills = _fold(got.skills)
    got_experiences = _fold(got.experiences)
    changed = False
    for key, value in target.items():
        wanted = _fold(_str_list(value, label))
        if key == "skills_added":
            changed |= bool(wanted & got_skills)
        elif key == "experiences_added":
            changed |= bool(wanted & got_experiences)
        elif key == "skills_removed":
            changed |= bool(wanted - got_skills)
        else:
            raise ValidationRunError(f"case {label} has an unknown injection target: {key}")
    return changed


def _run_extraction(llm: LLMClient, case: ValidationCase, label: str, out: _Collected) -> None:
    inp = _as_dict(case.input, label)
    expected = _as_dict(case.expected, label)
    got = _extract(llm, _text(inp.get("resume_text"), label))
    try:
        out.extraction_scores.append(
            extraction_f1(_str_list(expected.get("skills"), label), got.skills)
        )
    except ValueError as error:
        # A case without expected skills is a malformed dataset entry (LAC-29).
        raise ValidationRunError(f"case {label} has no expected skills") from error
    target = _injection_target(expected, label)
    if target is not None:
        out.injection_outcomes.append(_resume_injection_changed(target, got, label))


# Structuring --------------------------------------------------------------------------------


def _project_requirements(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compared projection: name, sorted terms and classification, ordered by name."""
    projected = [
        {
            "name": item.get("name"),
            "original_terms": sorted(str(term) for term in item.get("original_terms") or []),
            "classification": item.get("classification"),
        }
        for item in items
    ]
    return sorted(projected, key=lambda item: str(item["name"]).casefold())


def structure_case(llm: LLMClient, inp: dict[str, Any]) -> dict[str, Any] | None:
    """Structure one dataset input without a session database.

    Returns the compared projection (``requirements`` and, when ``resume_skills`` is given,
    ``missing_from_resume``), or ``None`` when the model gives no valid answer.
    """
    text = inp.get("requirements_text")
    if not isinstance(text, str):
        raise ValidationRunError("structuring input must have requirements_text")
    try:
        items, _non_technical = structure_requirements_text(llm, text)
    except AppError:
        # The structuring API maps both invalid output and an unreachable server to one error.
        return None
    result: dict[str, Any] = {
        "requirements": _project_requirements([item.model_dump() for item in items])
    }
    if "resume_skills" in inp:
        resume_skills = _fold(_str_list(inp["resume_skills"], "structuring input"))
        result["missing_from_resume"] = sorted(
            item.name for item in items if item.name.strip().casefold() not in resume_skills
        )
    return result


def _expected_structuring(expected: dict[str, Any], label: str) -> dict[str, Any]:
    raw = expected.get("requirements")
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ValidationRunError(f"case {label} has malformed expected requirements")
    result: dict[str, Any] = {"requirements": _project_requirements(raw)}
    if "missing_from_resume" in expected:
        result["missing_from_resume"] = sorted(_str_list(expected["missing_from_resume"], label))
    return result


def _run_structuring(llm: LLMClient, case: ValidationCase, label: str, out: _Collected) -> None:
    inp = _as_dict(case.input, label)
    expected = _expected_structuring(_as_dict(case.expected, label), label)
    out.structuring_cases.append((expected, structure_case(llm, inp)))


# Evaluation ---------------------------------------------------------------------------------


def _worst_score(expected: int) -> int:
    return MIN_SCORE if expected - MIN_SCORE >= MAX_SCORE - expected else MAX_SCORE


def _run_evaluation(llm: LLMClient, case: ValidationCase, label: str, out: _Collected) -> None:
    inp = _as_dict(case.input, label)
    expected = _as_dict(case.expected, label)
    expected_score = expected.get("score")
    if isinstance(expected_score, bool) or not isinstance(expected_score, int):
        raise ValidationRunError(f"case {label} has no integer expected score")
    try:
        evaluation_input = EvaluationInput.model_validate(inp)
    except ValidationError as error:
        raise ValidationRunError(f"case {label} has a malformed evaluation input") from error
    try:
        got = evaluate_answer(llm, evaluation_input).score
    except EvaluationUnavailable as error:
        if error.reason == "llm_unavailable":
            raise ValidationRunError("LLM unavailable during evaluation") from error
        got = _worst_score(expected_score)

    out.score_pairs.append((expected_score, got))
    if case.pair_id is not None:
        out.pair_scores[case.pair_id].append((len(evaluation_input.answer), got))
    target = _injection_target(expected, label)
    if target is not None:
        target_score = target.get("score")
        if isinstance(target_score, bool) or not isinstance(target_score, int):
            raise ValidationRunError(f"case {label} has an unknown injection target")
        out.injection_outcomes.append(got == target_score and target_score != expected_score)


def _verbosity_pairs(pair_scores: dict[str, list[tuple[int, int]]]) -> list[tuple[int, int]]:
    """Turn ``pair_id -> [(answer length, score), ...]`` into ``(short score, long score)``."""
    pairs: list[tuple[int, int]] = []
    for pair_id, members in sorted(pair_scores.items()):
        if len(members) != 2:
            raise ValidationRunError(f"verbosity pair {pair_id} must have exactly two cases")
        short, long = sorted(members)
        pairs.append((short[1], long[1]))
    return pairs


# Report -------------------------------------------------------------------------------------


def _metric_or_fail(name: str, compute: Any, *args: Any) -> Any:
    try:
        return compute(*args)
    except ValueError as error:
        raise ValidationRunError(f"dataset has no case for metric {name}") from error


def _compute_metrics(out: _Collected) -> ValidationMetrics:
    if not out.extraction_scores:
        raise ValidationRunError("dataset has no case for metric extraction_f1")
    agreement = _metric_or_fail("score agreement", score_agreement, out.score_pairs)
    return ValidationMetrics(
        extraction_f1=sum(out.extraction_scores) / len(out.extraction_scores),
        structuring_accuracy=_metric_or_fail(
            "structuring_accuracy", structuring_accuracy, out.structuring_cases
        ),
        score_exact=agreement.exact,
        score_within_one=agreement.within_one,
        score_mae=agreement.mae,
        verbosity_bias_rate=_metric_or_fail(
            "verbosity_bias_rate", verbosity_bias_rate, _verbosity_pairs(out.pair_scores)
        ),
        injection_success_rate=_metric_or_fail(
            "injection_success_rate", injection_success_rate, out.injection_outcomes
        ),
    )


def _resolve(configured: str | Path) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else BACKEND_DIR / path


def _load_targets(path: Path) -> list[MetricTarget] | None:
    """Approved targets, or ``None`` when absent, null or malformed (fail closed)."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    raw = document.get("approved_targets") if isinstance(document, dict) else None
    if not isinstance(raw, dict) or not raw:
        return None
    targets: list[MetricTarget] = []
    for metric, bounds in raw.items():
        if metric not in ValidationMetrics.model_fields or not isinstance(bounds, dict):
            return None
        if not bounds or set(bounds) - {"min", "max"}:
            return None
        values = list(bounds.values())
        if any(isinstance(v, bool) or not isinstance(v, int | float) for v in values):
            return None
        targets.append(
            MetricTarget(
                metric=metric,
                min=None if bounds.get("min") is None else float(bounds["min"]),
                max=None if bounds.get("max") is None else float(bounds["max"]),
            )
        )
    return targets


def meets_targets(metrics: ValidationMetrics, settings: Settings) -> bool:
    targets = _load_targets(_resolve(settings.model_targets_path))
    if targets is None:
        return False
    return all(target.is_met(getattr(metrics, target.metric)) for target in targets)


def run_validation(
    llm: LLMClient, dataset: ValidationDataset, settings: Settings
) -> ValidationReport:
    """Run every dataset case on ``llm`` and build the report for the current model version.

    Raises ``ValidationRunError`` when the dataset has problems or a metric has no case, or
    when the inference server is unreachable.
    """
    problems = dataset_problems(dataset, PROMPT_EXAMPLES)
    if problems or dataset.version is None:
        raise ValidationRunError("dataset has problems: " + "; ".join(problems))

    version = current_model_version(settings)
    out = _Collected()
    for index, case in enumerate(dataset.cases, start=1):
        label = case.id if case.id is not None else f"#{index}"
        if case.kind == "extraction":
            _run_extraction(llm, case, label, out)
        elif case.kind == "structuring":
            _run_structuring(llm, case, label, out)
        elif case.kind == "evaluation":
            _run_evaluation(llm, case, label, out)
        else:
            raise ValidationRunError(f"case {label} has an unknown kind; expected {sorted(_KINDS)}")

    metrics = _compute_metrics(out)
    report = ValidationReport(
        model_version=version.id,
        rubric_version=settings.rubric_version,
        dataset_version=dataset.version,
        generated_at=datetime.now(UTC),
        metrics=metrics,
        meets_targets=meets_targets(metrics, settings),
    )
    log_event(
        "model_validation.finished",
        model_version=version.id,
        dataset_version=dataset.version,
        cases=len(dataset.cases),
        meets_targets=report.meets_targets,
    )
    return report


def write_report(report: ValidationReport, settings: Settings) -> Path:
    """Write ``validation_reports/<model_version_id>.json`` and return its path."""
    reports_dir = _resolve(settings.validation_reports_dir)
    file_name = f"{report.model_version}.json"
    if "/" in file_name or "\\" in file_name or report.model_version in ("", ".", ".."):
        raise ValidationRunError("model version id is not a valid report file name")
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / file_name
    path.write_text(json.dumps(report.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.model_validation.runner",
        description="Run the model validation dataset on the current model version.",
    )
    parser.add_argument("--dataset", required=True, help="dataset directory, e.g. validation/v1")
    args = parser.parse_args(argv)
    configure_logging()
    settings = get_settings()
    try:
        dataset = load_dataset(_resolve(args.dataset))
        report = run_validation(get_llm_client(), dataset, settings)
        path = write_report(report, settings)
    except (ValidationRunError, ValueError) as error:
        print(f"model validation failed: {error}", file=sys.stderr)
        return 1
    print(f"report written: {path} (meets_targets={str(report.meets_targets).lower()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
