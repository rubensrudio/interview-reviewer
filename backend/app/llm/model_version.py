"""Model version identification and production release gate (MODEL-03, MODEL-04).

A model version is the base model plus the configuration version (instructions and
parameters); weights are never fine-tuned, so nothing else identifies it (LAC-02).

In production the application only starts when `validation_reports/<model_version_id>.json`
exists for the current version, declares `meets_targets`, and every metric satisfies the
targets approved in `model_targets.yaml`. While `approved_targets` is null (LAC-03 pending)
the gate always refuses. Any missing or malformed input also refuses (fail closed).
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from app.config import Settings
from app.observability import log_event

BACKEND_DIR = Path(__file__).resolve().parents[2]

_BOUND_KEYS = frozenset({"min", "max"})


class ModelNotApproved(Exception):
    """The current model version has no validation report that meets the approved targets."""


@dataclass(frozen=True)
class ModelVersion:
    base_model: str
    config_version: str
    id: str


class ValidationMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extraction_f1: float
    structuring_accuracy: float
    score_exact: float
    score_within_one: float
    score_mae: float
    verbosity_bias_rate: float
    injection_success_rate: float


class ValidationReport(BaseModel):
    """Format of `validation_reports/<model_version_id>.json` (CT-56)."""

    model_config = ConfigDict(extra="forbid")

    model_version: str
    rubric_version: str
    dataset_version: str
    generated_at: datetime
    metrics: ValidationMetrics
    meets_targets: bool


@dataclass(frozen=True)
class MetricTarget:
    metric: str
    min: float | None = None
    max: float | None = None

    def is_met(self, value: float) -> bool:
        if self.min is not None and value < self.min:
            return False
        return self.max is None or value <= self.max


def current_model_version(settings: Settings) -> ModelVersion:
    base_model = settings.llm_model
    config_version = settings.llm_config_version
    return ModelVersion(
        base_model=base_model,
        config_version=config_version,
        id=f"{base_model}+{config_version}",
    )


def assert_model_release_allowed(settings: Settings) -> None:
    """Raise `ModelNotApproved` unless the current version passed validation against targets."""
    version = current_model_version(settings)
    try:
        targets = _load_targets(_resolve(settings.model_targets_path))
        report = _load_report(_resolve(settings.validation_reports_dir), version.id)
        _check_report(report, version, targets)
    except ModelNotApproved as error:
        log_event(
            "model.release_refused",
            model_version=version.id,
            reason=str(error),
        )
        raise
    log_event("model.release_allowed", model_version=version.id)


def _resolve(configured: str) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else BACKEND_DIR / path


def _load_targets(path: Path) -> list[MetricTarget]:
    try:
        with path.open(encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as error:
        raise ModelNotApproved("model targets file is missing or unreadable") from error
    if not isinstance(document, dict) or "approved_targets" not in document:
        raise ModelNotApproved("model targets file must define 'approved_targets'")

    raw_targets = document["approved_targets"]
    if raw_targets is None:
        raise ModelNotApproved("approved_targets is null: no model targets approved yet")
    if not isinstance(raw_targets, dict) or not raw_targets:
        raise ModelNotApproved("approved_targets must be a non-empty mapping")
    return [_parse_target(metric, bounds) for metric, bounds in raw_targets.items()]


def _parse_target(metric: Any, bounds: Any) -> MetricTarget:
    if not isinstance(metric, str) or metric not in ValidationMetrics.model_fields:
        raise ModelNotApproved(f"unknown target metric: {metric!r}")
    if not isinstance(bounds, dict) or not bounds or set(bounds) - _BOUND_KEYS:
        raise ModelNotApproved(f"target {metric} must define 'min' and/or 'max'")
    values: dict[str, float] = {}
    for key, value in bounds.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ModelNotApproved(f"target {metric}.{key} must be a number")
        values[key] = float(value)
    return MetricTarget(metric=metric, min=values.get("min"), max=values.get("max"))


def _load_report(reports_dir: Path, version_id: str) -> ValidationReport:
    reports_root = reports_dir.resolve()
    path = (reports_root / f"{version_id}.json").resolve()
    if path.parent != reports_root:
        raise ModelNotApproved("model version id is not a valid report file name")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ModelNotApproved("no validation report for the current model version") from error
    try:
        return ValidationReport.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as error:
        raise ModelNotApproved("validation report is malformed") from error


def _check_report(
    report: ValidationReport, version: ModelVersion, targets: list[MetricTarget]
) -> None:
    if report.model_version != version.id:
        raise ModelNotApproved("validation report belongs to another model version")
    if not report.meets_targets:
        raise ModelNotApproved("validation report does not meet targets")
    for target in targets:
        value = getattr(report.metrics, target.metric)
        if not target.is_met(value):
            raise ModelNotApproved(f"metric {target.metric} does not meet its approved target")
