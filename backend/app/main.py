import os

from fastapi import FastAPI

from app.api import account, auth_google, auth_local, resumes
from app.config import Settings, get_settings
from app.errors import register_error_handlers
from app.llm.model_version import assert_model_release_allowed
from app.logging_setup import configure_logging

PRODUCTION = "production"


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    _enforce_model_release_gate(settings)
    app = FastAPI(title="Interview Reviewer")
    register_error_handlers(app)
    app.include_router(auth_local.router)
    app.include_router(auth_google.router)
    app.include_router(account.router)
    app.include_router(resumes.router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _enforce_model_release_gate(settings: Settings | None) -> None:
    """Refuse to build the app in production without an approved model version (MODEL-03)."""
    if settings is None:
        # Full settings require secrets; load them here only when production is requested.
        # Env names are matched case-insensitively, like pydantic-settings does.
        requested = [value for name, value in os.environ.items() if name.upper() == "IR_APP_ENV"]
        if PRODUCTION not in requested:
            return
        settings = get_settings()
    if settings.app_env == PRODUCTION:
        assert_model_release_allowed(settings)
