from fastapi import FastAPI

from app.errors import register_error_handlers
from app.logging_setup import configure_logging


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Interview Reviewer")
    register_error_handlers(app)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
