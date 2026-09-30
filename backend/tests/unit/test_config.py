from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings

REQUIRED_ENV = {
    "IR_THROTTLE_SECRET": "test-throttle-secret",
    "IR_OIDC_STATE_SECRET": "test-oidc-state-secret",
}


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cv_01_default_max_pdf_bytes_is_5_mb() -> None:
    assert get_settings().max_pdf_bytes == 5242880


def test_cv_04_default_max_resumes_per_user_is_10() -> None:
    assert get_settings().max_resumes_per_user == 10


def test_intv_05_default_max_answer_chars_is_5000() -> None:
    assert get_settings().max_answer_chars == 5000


def test_intv_13_default_session_expiry_days_is_30() -> None:
    assert get_settings().session_expiry_days == 30


def test_default_max_required_skills_is_20_and_ocr_is_disabled() -> None:
    settings = get_settings()
    assert settings.max_required_skills == 20
    assert settings.ocr_enabled is False


def test_know_01_default_llm_settings_point_to_private_host() -> None:
    settings = get_settings()
    assert settings.llm_base_url == "http://localhost:11434/v1"
    assert settings.llm_allowed_hosts == ["localhost", "127.0.0.1", "llm"]
    assert settings.llm_max_attempts == 3
    assert settings.llm_timeout_seconds == 120


def test_cv_01_env_var_overrides_max_pdf_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IR_MAX_PDF_BYTES", "1000")
    assert get_settings().max_pdf_bytes == 1000


def test_env_list_value_overrides_llm_allowed_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IR_LLM_ALLOWED_HOSTS", '["llm-internal"]')
    assert get_settings().llm_allowed_hosts == ["llm-internal"]


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_missing_throttle_secret_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IR_THROTTLE_SECRET")
    with pytest.raises(ValidationError):
        Settings()


def test_empty_oidc_state_secret_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IR_OIDC_STATE_SECRET", "")
    with pytest.raises(ValidationError):
        Settings()


def test_secrets_are_not_exposed_in_repr() -> None:
    settings = get_settings()
    assert "test-throttle-secret" not in repr(settings)
    assert settings.throttle_secret.get_secret_value() == "test-throttle-secret"


def test_google_credentials_are_optional() -> None:
    settings = get_settings()
    assert settings.google_client_id is None
    assert settings.google_client_secret is None


def test_all_fields_are_listed_in_env_example() -> None:
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text()
    names = {line.split("=", 1)[0].strip() for line in example.splitlines() if "=" in line}
    expected = {f"IR_{field.upper()}" for field in Settings.model_fields}
    assert expected <= names
