"""The worker enforces the production model release gate before consuming jobs (MODEL-03)."""

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.jobs import worker as worker_module
from app.llm.model_version import current_model_version

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
    "score_mae": {"max": 0.50},
}

THROTTLE_TEST_VALUE = "worker-gate-throttle-secret"
OIDC_STATE_TEST_VALUE = "worker-gate-oidc-secret"


class FakeWorker:
    """Records whether the loop started; never touches the database."""

    instances: list["FakeWorker"] = []

    def __init__(self) -> None:
        self.ran = False
        FakeWorker.instances.append(self)

    def run_forever(self, stop: threading.Event) -> None:
        self.ran = True


def _settings(tmp_path: Path, app_env: str) -> Settings:
    return Settings(
        app_env=app_env,
        throttle_secret=SecretStr(THROTTLE_TEST_VALUE),
        oidc_state_secret=SecretStr(OIDC_STATE_TEST_VALUE),
        llm_model="qwen2.5:7b-instruct",
        llm_config_version="cfg-unapproved",
        model_targets_path=str(tmp_path / "model_targets.yaml"),
        validation_reports_dir=str(tmp_path / "validation_reports"),
    )


def _write_targets(settings: Settings) -> None:
    Path(settings.model_targets_path).write_text(
        json.dumps({"approved_targets": TARGETS}), encoding="utf-8"
    )


def _write_passing_report(settings: Settings) -> None:
    reports_dir = Path(settings.validation_reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    version_id = current_model_version(settings).id
    report = {
        "model_version": version_id,
        "rubric_version": "rubric-1",
        "dataset_version": "v1",
        "generated_at": "2026-09-30T12:00:00Z",
        "metrics": PASSING_METRICS,
        "meets_targets": True,
    }
    (reports_dir / f"{version_id}.json").write_text(json.dumps(report), encoding="utf-8")


@pytest.fixture
def run_main(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _run(settings: Settings) -> None:
        FakeWorker.instances = []
        monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
        monkeypatch.setattr(worker_module, "Worker", FakeWorker)
        monkeypatch.setattr(worker_module.signal, "signal", lambda *args: None)
        worker_module.main()

    return _run


def test_model_03_worker_in_production_without_report_exits_before_any_job(
    tmp_path: Path, run_main: Any, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path, "production")
    _write_targets(settings)

    with pytest.raises(SystemExit) as exit_info:
        run_main(settings)

    assert exit_info.value.code not in (0, None)
    assert FakeWorker.instances == []
    assert "worker.release_refused" in [record.getMessage() for record in caplog.records]
    logged = " ".join(str(record.__dict__) for record in caplog.records)
    assert THROTTLE_TEST_VALUE not in logged
    assert OIDC_STATE_TEST_VALUE not in logged


def test_model_03_worker_in_production_with_approved_report_starts(
    tmp_path: Path, run_main: Any
) -> None:
    settings = _settings(tmp_path, "production")
    _write_targets(settings)
    _write_passing_report(settings)

    run_main(settings)

    assert [worker.ran for worker in FakeWorker.instances] == [True]


def test_model_03_worker_in_development_without_report_starts(
    tmp_path: Path, run_main: Any
) -> None:
    settings = _settings(tmp_path, "development")

    run_main(settings)

    assert [worker.ran for worker in FakeWorker.instances] == [True]
