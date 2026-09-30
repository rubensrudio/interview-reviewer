from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Interview Reviewer")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
