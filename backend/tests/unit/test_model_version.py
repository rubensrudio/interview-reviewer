import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.config import Settings, get_settings
from app.llm.model_version import (
    ModelNotApproved,
    ModelVersion,
    ValidationReport,
    assert_model_release_allowed,
    current_model_version,
)
from app.main import create_app

BACKEND_DIR = Path(__file__).resolve().parents[2]

PASSING_METRICS = {
    "extraction_f1": 0.92,
    "structuring_accuracy": 0.95,
    "score_exact": 0.70,
    "score_within_one": 0.93,
    "score_mae": 0.40,
    "verbosity_bias_rate": 0.02,
    "injection_success_rate": 0.0,
}

TARGETS = {
    "extraction_f1": {"min": 0.90},
    "score_within_one": {"min": 0.90},
    "score_mae": {"max": 0.50},
    "injection_success_rate": {"max": 0.01},
}


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(tmp_path: Path, app_env: str = "production", **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": app_env,
        "throttle_secret": SecretStr("test-throttle-secret"),
        "oidc_state_secret": SecretStr("test-oidc-state-secret"),
        "llm_model": "qwen2.5:7b-instruct",
        "llm_config_version": "cfg-1",
        "model_targets_path": str(tmp_path / "model_targets.yaml"),
        "validation_reports_dir": str(tmp_path / "validation_reports"),
    }
    values.update(overrides)
    return Settings(**values)


def _write_targets(settings: Settings, targets: dict[str, Any] | None) -> None:
    Path(settings.model_targets_path).write_text(
        json.dumps({"approved_targets": targets}), encoding="utf-8"
    )


def _write_report(
    settings: Settings,
    *,
    metrics: dict[str, float] | None = None,
    meets_targets: bool = True,
    model_version: str | None = None,
) -> None:
    reports_dir = Path(settings.validation_reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    version_id = current_model_version(settings).id
    report = {
        "model_version": model_version or version_id,
        "rubric_version": "rubric-1",
        "dataset_version": "v1",
        "generated_at": "2026-09-30T12:00:00Z",
        "metrics": metrics or PASSING_METRICS,
        "meets_targets": meets_targets,
    }
    (reports_dir / f"{version_id}.json").write_text(json.dumps(report), encoding="utf-8")


def test_model_04_version_identifies_base_model_and_config_version(tmp_path: Path) -> None:
    settings = _settings(tmp_path, llm_model="qwen2.5:7b-instruct", llm_config_version="cfg-7")

    version = current_model_version(settings)

    assert version == ModelVersion(
        base_model="qwen2.5:7b-instruct",
        config_version="cfg-7",
        id="qwen2.5:7b-instruct+cfg-7",
    )


def test_model_03_production_without_report_refuses_to_create_app(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_targets(settings, TARGETS)

    with pytest.raises(ModelNotApproved):
        create_app(settings)


def test_model_03_production_with_metric_below_target_refuses_to_create_app(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _write_targets(settings, TARGETS)
    _write_report(settings, metrics={**PASSING_METRICS, "extraction_f1": 0.85})

    with pytest.raises(ModelNotApproved):
        create_app(settings)


def test_model_03_production_with_metric_above_max_target_refuses(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_targets(settings, TARGETS)
    _write_report(settings, metrics={**PASSING_METRICS, "score_mae": 0.75})

    with pytest.raises(ModelNotApproved):
        assert_model_release_allowed(settings)


def test_model_03_production_with_report_meeting_all_targets_creates_app(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _write_targets(settings, TARGETS)
    _write_report(settings)

    app = create_app(settings)

    assert app is not None


def test_model_03_development_without_report_creates_app(tmp_path: Path) -> None:
    settings = _settings(tmp_path, app_env="development")

    app = create_app(settings)

    assert app is not None


def test_model_03_development_from_environment_creates_app_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IR_APP_ENV", raising=False)
    monkeypatch.delenv("IR_THROTTLE_SECRET", raising=False)
    monkeypatch.delenv("IR_OIDC_STATE_SECRET", raising=False)

    assert create_app() is not None


def test_model_03_production_from_environment_applies_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IR_APP_ENV", "production")
    monkeypatch.setenv("IR_THROTTLE_SECRET", "test-throttle-secret")
    monkeypatch.setenv("IR_OIDC_STATE_SECRET", "test-oidc-state-secret")
    monkeypatch.setenv("IR_MODEL_TARGETS_PATH", str(tmp_path / "model_targets.yaml"))
    monkeypatch.setenv("IR_VALIDATION_REPORTS_DIR", str(tmp_path / "validation_reports"))
    _write_targets(get_settings(), TARGETS)

    with pytest.raises(ModelNotApproved):
        create_app()


def test_model_03_null_approved_targets_always_refuses(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_targets(settings, None)
    _write_report(settings)

    with pytest.raises(ModelNotApproved):
        assert_model_release_allowed(settings)


def test_model_03_report_with_meets_targets_false_refuses(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_targets(settings, TARGETS)
    _write_report(settings, meets_targets=False)

    with pytest.raises(ModelNotApproved):
        assert_model_release_allowed(settings)


def test_model_03_report_for_another_model_version_refuses(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_targets(settings, TARGETS)
    _write_report(settings, model_version="other-model+cfg-1")

    with pytest.raises(ModelNotApproved):
        assert_model_release_allowed(settings)


def test_model_03_malformed_report_refuses(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_targets(settings, TARGETS)
    reports_dir = Path(settings.validation_reports_dir)
    reports_dir.mkdir()
    (reports_dir / f"{current_model_version(settings).id}.json").write_text(
        "{not json", encoding="utf-8"
    )

    with pytest.raises(ModelNotApproved):
        assert_model_release_allowed(settings)


def test_model_03_unknown_target_metric_refuses(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_targets(settings, {"made_up_metric": {"min": 0.5}})
    _write_report(settings)

    with pytest.raises(ModelNotApproved):
        assert_model_release_allowed(settings)


def test_model_03_missing_targets_file_refuses(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_report(settings)

    with pytest.raises(ModelNotApproved):
        assert_model_release_allowed(settings)


def test_model_03_model_version_id_escaping_reports_dir_refuses(tmp_path: Path) -> None:
    settings = _settings(tmp_path, llm_model="../../etc/passwd")
    _write_targets(settings, TARGETS)

    with pytest.raises(ModelNotApproved):
        assert_model_release_allowed(settings)


def test_model_03_shipped_targets_file_has_null_approved_targets(tmp_path: Path) -> None:
    settings = _settings(tmp_path, model_targets_path="config/model_targets.yaml")
    _write_report(settings)

    with pytest.raises(ModelNotApproved, match="approved_targets"):
        assert_model_release_allowed(settings)
    assert (BACKEND_DIR / "config" / "model_targets.yaml").is_file()


def test_validation_report_parses_contract_format() -> None:
    report = ValidationReport.model_validate(
        {
            "model_version": "qwen2.5:7b-instruct+cfg-1",
            "rubric_version": "rubric-1",
            "dataset_version": "v1",
            "generated_at": "2026-09-30T12:00:00Z",
            "metrics": PASSING_METRICS,
            "meets_targets": True,
        }
    )

    assert report.metrics.extraction_f1 == 0.92
    assert report.meets_targets is True
