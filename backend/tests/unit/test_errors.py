import re

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app import errors
from app.errors import CATALOG, AppError, register_error_handlers
from app.main import create_app

PLAN_CODES = {
    "VALIDATION_ERROR",
    "AUTH_REQUIRED",
    "TERMS_REQUIRED",
    "CSRF_FAILED",
    "RESOURCE_NOT_FOUND",
    "PASSWORD_POLICY",
    "TERMS_NOT_ACCEPTED",
    "INVALID_CREDENTIALS",
    "EMAIL_NOT_VERIFIED",
    "TOO_MANY_ATTEMPTS",
    "LINK_INVALID",
    "GOOGLE_AUTH_FAILED",
    "INVALID_PDF",
    "FILE_TOO_LARGE",
    "RESUME_LIMIT_REACHED",
    "RESUME_NOT_READY",
    "SESSION_IN_PROGRESS",
    "LANGUAGE_NOT_SUPPORTED",
    "INVALID_STATE",
    "EMPTY_REQUIREMENTS",
    "NO_REQUIRED_SKILLS",
    "TOO_MANY_REQUIRED_SKILLS",
    "PENDING_CLARIFICATION",
    "EMPTY_ANSWER",
    "ANSWER_TOO_LONG",
    "QUESTION_ALREADY_ANSWERED",
    "NOT_CURRENT_QUESTION",
    "SESSION_CLOSED",
    "CLARIFICATION_UNAVAILABLE",
    "LLM_UNAVAILABLE",
    "REPORT_NOT_AVAILABLE",
}


class _Payload(BaseModel):
    name: str
    age: int


def _client() -> TestClient:
    app = create_app()

    @app.get("/test/missing")
    def missing() -> None:
        raise AppError("RESOURCE_NOT_FOUND", 404, "Not found")

    @app.get("/test/details")
    def with_details() -> None:
        raise AppError("INVALID_STATE", 409, "Nope", {"state": "draft"})

    @app.post("/test/body")
    def body(payload: _Payload) -> dict[str, str]:
        return {"name": payload.name}

    @app.get("/test/catalog")
    def catalog() -> None:
        raise AppError.from_catalog(errors.TOO_MANY_REQUIRED_SKILLS, count=23, excess=3)

    return TestClient(app)


def test_auth16_app_error_produces_exact_envelope() -> None:
    response = _client().get("/test/missing")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "RESOURCE_NOT_FOUND", "message": "Not found", "details": None}
    }


def test_app_error_keeps_details() -> None:
    response = _client().get("/test/details")

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "INVALID_STATE", "message": "Nope", "details": {"state": "draft"}}
    }


def test_invalid_body_returns_validation_error_envelope() -> None:
    response = _client().post("/test/body", json={"name": "x", "age": "secret-value"})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "Please check the highlighted fields."
    fields = body["error"]["details"]["fields"]
    assert fields[0]["loc"] == ["body", "age"]
    assert "secret-value" not in response.text


def test_all_plan_codes_exist_as_constants() -> None:
    for code in PLAN_CODES:
        assert getattr(errors, code) == code
    assert set(CATALOG) == PLAN_CODES


def test_catalog_statuses_match_plan() -> None:
    assert CATALOG[errors.RESOURCE_NOT_FOUND].status == 404
    assert CATALOG[errors.VALIDATION_ERROR].status == 422
    assert CATALOG[errors.INVALID_CREDENTIALS].status == 401
    assert CATALOG[errors.TOO_MANY_ATTEMPTS].status == 429
    assert CATALOG[errors.GOOGLE_AUTH_FAILED].status == 302
    assert CATALOG[errors.LLM_UNAVAILABLE].status == 503


def test_auth05_invalid_credentials_message_is_generic() -> None:
    error = AppError.from_catalog(errors.INVALID_CREDENTIALS)

    assert error.status == 401
    assert error.message == "Invalid e-mail or password."


def test_from_catalog_formats_placeholders() -> None:
    response = _client().get("/test/catalog")

    assert response.status_code == 422
    assert response.json()["error"]["message"] == (
        "This job lists 23 required skills. The limit is 20 — review the list and "
        "remove or merge 3 before continuing."
    )


def test_register_error_handlers_on_bare_app() -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/x")
    def x() -> None:
        raise AppError.from_catalog(errors.RESOURCE_NOT_FOUND)

    response = TestClient(app).get("/x")
    assert response.json() == {
        "error": {"code": "RESOURCE_NOT_FOUND", "message": "Not found.", "details": None}
    }


def test_no_placeholders_left_unformatted_in_plain_messages() -> None:
    for code, entry in CATALOG.items():
        if code != errors.TOO_MANY_REQUIRED_SKILLS:
            assert not re.search(r"\{\w+\}", entry.message), code
