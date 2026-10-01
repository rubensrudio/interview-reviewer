"""Error catalog (plan section 8.3) and the JSON error envelope (CT-3).

Every API error is returned as ``{"error": {"code", "message", "details"}}``.
"""

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

VALIDATION_ERROR = "VALIDATION_ERROR"
AUTH_REQUIRED = "AUTH_REQUIRED"
TERMS_REQUIRED = "TERMS_REQUIRED"
CSRF_FAILED = "CSRF_FAILED"
RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
PASSWORD_POLICY = "PASSWORD_POLICY"  # noqa: S105 - error code, not a secret
TERMS_NOT_ACCEPTED = "TERMS_NOT_ACCEPTED"
INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
EMAIL_NOT_VERIFIED = "EMAIL_NOT_VERIFIED"
TOO_MANY_ATTEMPTS = "TOO_MANY_ATTEMPTS"
LINK_INVALID = "LINK_INVALID"
GOOGLE_AUTH_FAILED = "GOOGLE_AUTH_FAILED"
INVALID_PDF = "INVALID_PDF"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
RESUME_LIMIT_REACHED = "RESUME_LIMIT_REACHED"
RESUME_NOT_READY = "RESUME_NOT_READY"
SESSION_IN_PROGRESS = "SESSION_IN_PROGRESS"
LANGUAGE_NOT_SUPPORTED = "LANGUAGE_NOT_SUPPORTED"
INVALID_STATE = "INVALID_STATE"
EMPTY_REQUIREMENTS = "EMPTY_REQUIREMENTS"
NO_REQUIRED_SKILLS = "NO_REQUIRED_SKILLS"
TOO_MANY_REQUIRED_SKILLS = "TOO_MANY_REQUIRED_SKILLS"
PENDING_CLARIFICATION = "PENDING_CLARIFICATION"
EMPTY_ANSWER = "EMPTY_ANSWER"
ANSWER_TOO_LONG = "ANSWER_TOO_LONG"
QUESTION_ALREADY_ANSWERED = "QUESTION_ALREADY_ANSWERED"
NOT_CURRENT_QUESTION = "NOT_CURRENT_QUESTION"
SESSION_CLOSED = "SESSION_CLOSED"
CLARIFICATION_UNAVAILABLE = "CLARIFICATION_UNAVAILABLE"
LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
REPORT_NOT_AVAILABLE = "REPORT_NOT_AVAILABLE"


@dataclass(frozen=True)
class CatalogEntry:
    status: int
    message: str


_ALREADY_ANSWERED_MESSAGE = "This question has already been answered. Showing the current question."

# Codes whose catalog entry has no user message (the client redirects instead)
# carry an empty message on purpose: no text outside the catalog is invented.
CATALOG: dict[str, CatalogEntry] = {
    VALIDATION_ERROR: CatalogEntry(422, "Please check the highlighted fields."),
    AUTH_REQUIRED: CatalogEntry(401, ""),
    TERMS_REQUIRED: CatalogEntry(403, ""),
    CSRF_FAILED: CatalogEntry(403, "Your session expired. Please reload the page."),
    RESOURCE_NOT_FOUND: CatalogEntry(404, "Not found."),
    PASSWORD_POLICY: CatalogEntry(
        422,
        "Password must have at least 8 characters and must not be a common password.",
    ),
    TERMS_NOT_ACCEPTED: CatalogEntry(
        422, "You must accept the Terms of Use and the Privacy Policy."
    ),
    INVALID_CREDENTIALS: CatalogEntry(401, "Invalid e-mail or password."),
    EMAIL_NOT_VERIFIED: CatalogEntry(
        403, "Please verify your e-mail before signing in. Resend verification e-mail?"
    ),
    TOO_MANY_ATTEMPTS: CatalogEntry(429, "Too many attempts. Please try again later."),
    LINK_INVALID: CatalogEntry(400, "This link is invalid or has expired. Request a new one."),
    GOOGLE_AUTH_FAILED: CatalogEntry(
        302, "Google sign-in failed or was cancelled. Please try again."
    ),
    INVALID_PDF: CatalogEntry(415, "Please upload a valid PDF file."),
    FILE_TOO_LARGE: CatalogEntry(413, "The file exceeds the 5 MB limit."),
    RESUME_LIMIT_REACHED: CatalogEntry(
        409, "You have reached the limit of 10 resumes. Delete one to upload another."
    ),
    RESUME_NOT_READY: CatalogEntry(409, "This resume is not ready yet."),
    SESSION_IN_PROGRESS: CatalogEntry(
        409,
        "You already have an interview in progress. Resume or cancel it to start a new one.",
    ),
    LANGUAGE_NOT_SUPPORTED: CatalogEntry(422, "This language is not supported yet."),
    INVALID_STATE: CatalogEntry(409, "This action is not available at this step."),
    EMPTY_REQUIREMENTS: CatalogEntry(
        422,
        "Please paste the job requirements for the position you are preparing for.",
    ),
    NO_REQUIRED_SKILLS: CatalogEntry(
        422, "Define at least one required technical skill to continue."
    ),
    TOO_MANY_REQUIRED_SKILLS: CatalogEntry(
        422,
        "This job lists {count} required skills. The limit is 20 — review the list "
        "and remove or merge {excess} before continuing.",
    ),
    PENDING_CLARIFICATION: CatalogEntry(
        422, "Some requirements need clarification before you continue."
    ),
    EMPTY_ANSWER: CatalogEntry(422, "Please type an answer before submitting."),
    ANSWER_TOO_LONG: CatalogEntry(422, "Answers are limited to 5,000 characters."),
    QUESTION_ALREADY_ANSWERED: CatalogEntry(409, _ALREADY_ANSWERED_MESSAGE),
    NOT_CURRENT_QUESTION: CatalogEntry(409, _ALREADY_ANSWERED_MESSAGE),
    SESSION_CLOSED: CatalogEntry(409, "This interview is closed."),
    CLARIFICATION_UNAVAILABLE: CatalogEntry(
        503,
        "Clarifications are temporarily unavailable. You can still submit your answer.",
    ),
    LLM_UNAVAILABLE: CatalogEntry(
        503, "The assistant is temporarily unavailable. Please try again."
    ),
    REPORT_NOT_AVAILABLE: CatalogEntry(409, "The report is not available for this interview."),
}


class AppError(Exception):
    """Application error rendered as the JSON error envelope (CT-3)."""

    def __init__(
        self,
        code: str,
        status: int,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.message = message
        self.details = details

    @classmethod
    def from_catalog(
        cls,
        code: str,
        details: dict[str, Any] | None = None,
        **message_args: object,
    ) -> "AppError":
        """Build an error using the status and message of the section 8.3 catalog."""
        entry = CATALOG[code]
        message = entry.message.format(**message_args) if message_args else entry.message
        return cls(code, entry.status, message, details)


def error_envelope(code: str, message: str, details: dict[str, Any] | None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


async def _app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)  # noqa: S101 - registered only for AppError
    return JSONResponse(
        status_code=exc.status,
        content=error_envelope(exc.code, exc.message, exc.details),
    )


async def _validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered only for it
    # Only location, message and type are exposed: the submitted input is never echoed.
    fields = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")}
        for err in exc.errors()
    ]
    entry = CATALOG[VALIDATION_ERROR]
    return JSONResponse(
        status_code=entry.status,
        content=error_envelope(VALIDATION_ERROR, entry.message, {"fields": fields}),
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
