"""Typed application settings loaded from `IR_`-prefixed environment variables."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IR_", extra="ignore")

    # Application
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://ir:ir@localhost:5432/interview_reviewer"
    storage_dir: str = "./storage"
    frontend_base_url: str = "http://localhost:4200"
    cookie_secure: bool = False

    # Authentication and tokens
    session_ttl_hours: int = Field(default=168, gt=0)
    verification_ttl_minutes: int = Field(default=1440, gt=0)
    reset_ttl_minutes: int = Field(default=60, gt=0)
    google_link_ttl_minutes: int = Field(default=15, gt=0)
    login_max_failures: int = Field(default=5, gt=0)
    login_lock_minutes: int = Field(default=15, gt=0)
    throttle_secret: SecretStr = Field(min_length=1)

    # SMTP
    smtp_host: str = "localhost"
    smtp_port: int = Field(default=1025, gt=0, le=65535)
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = "no-reply@interview-reviewer.local"
    smtp_starttls: bool = False

    # Google sign-in
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_redirect_uri: str = "http://localhost:4200/api/auth/google/callback"
    oidc_state_secret: SecretStr = Field(min_length=1)

    # Background jobs
    job_poll_seconds: int = Field(default=2, gt=0)

    # LLM
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "qwen2.5:7b-instruct"
    llm_allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "llm"]
    )
    llm_timeout_seconds: int = Field(default=120, gt=0)
    llm_max_attempts: int = Field(default=3, gt=0)
    llm_config_version: str = "cfg-1"
    rubric_version: str = "rubric-1"

    # Legal documents
    terms_version: str = "terms-2026-09"
    privacy_version: str = "privacy-2026-09"

    # Limits
    max_pdf_bytes: int = Field(default=5242880, gt=0)
    max_resumes_per_user: int = Field(default=10, gt=0)
    max_answer_chars: int = Field(default=5000, gt=0)
    session_expiry_days: int = Field(default=30, gt=0)
    max_required_skills: int = Field(default=20, gt=0)
    min_resume_text_chars: int = Field(default=200, ge=0)
    ocr_enabled: bool = False

    # Knowledge base, model targets and validation
    knowledge_sources_path: str = "config/approved_sources.yaml"
    knowledge_min_rank: float = Field(default=0.01, ge=0)
    model_targets_path: str = "config/model_targets.yaml"
    validation_reports_dir: str = "validation_reports"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
