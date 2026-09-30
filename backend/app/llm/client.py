"""Client for the private OpenAI-compatible inference server (CT-19, DA-2, KNOW-01, KNOW-92).

The backend talks to the self-hosted LLM (vLLM in production, Ollama in development) only
through ``httpx`` on ``/chat/completions``, asking for a JSON answer that follows the JSON
schema of a pydantic model. The client refuses, at construction time, any base URL whose host
is not in ``llm_allowed_hosts``, so no inference can reach an external provider (LAC-01).

Errors are mapped to two exceptions so every flow can apply its own failure state:
``LLMUnavailable`` (timeout, connection error, non-2xx status) and ``LLMInvalidOutput`` (the
answer is not JSON or does not validate against the output model). Prompts, answers and
server response bodies are never logged nor copied into exception messages.
"""

import json
import re
from collections.abc import Callable
from typing import Protocol

import httpx
from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.observability import log_event, timed

__all__ = [
    "HttpLLMClient",
    "LLMClient",
    "LLMInvalidOutput",
    "LLMUnavailable",
    "get_llm_client",
    "run_with_attempts",
]

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_TASK_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SCHEMA_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


class LLMUnavailable(Exception):
    """The inference server could not be reached or did not answer successfully."""


class LLMInvalidOutput(Exception):
    """The inference server answered, but not with JSON matching the output model."""


class LLMClient(Protocol):
    """Structured completion interface shared by the HTTP client and test fakes."""

    def complete_structured[T: BaseModel](
        self, task: str, system: str, user: str, output_model: type[T]
    ) -> T: ...


def _normalize_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


def _validated_base_url(base_url: str, allowed_hosts: list[str]) -> str:
    """Return ``base_url`` without a trailing slash, or raise if its host is not allowed."""
    try:
        url = httpx.URL(base_url)
    except httpx.InvalidURL as error:
        raise ValueError("LLM base URL is not a valid URL") from error
    if url.scheme not in _ALLOWED_SCHEMES or not url.host:
        raise ValueError("LLM base URL must be an absolute http(s) URL")
    if url.userinfo:
        raise ValueError("LLM base URL must not carry credentials")
    allowed = {_normalize_host(host) for host in allowed_hosts if host.strip()}
    if _normalize_host(url.host) not in allowed:
        # The host itself is safe to report: it comes from configuration, not user data.
        raise ValueError(f"LLM host {url.host!r} is not in llm_allowed_hosts")
    return base_url.rstrip("/")


class HttpLLMClient:
    """``LLMClient`` backed by an OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        allowed_hosts: list[str],
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._endpoint = f"{_validated_base_url(base_url, allowed_hosts)}/chat/completions"
        self._model = model
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    def complete_structured[T: BaseModel](
        self, task: str, system: str, user: str, output_model: type[T]
    ) -> T:
        if not _TASK_RE.fullmatch(task):
            raise ValueError("invalid LLM task name")
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": _SCHEMA_NAME_RE.sub("_", output_model.__name__)[:64],
                    "schema": output_model.model_json_schema(),
                },
            },
            "stream": False,
        }
        with timed("llm.inference", task=task):
            content = self._post(task, payload)
            return self._parse(task, content, output_model)

    def _post(self, task: str, payload: dict[str, object]) -> str:
        try:
            with httpx.Client(
                timeout=self._timeout, transport=self._transport, follow_redirects=False
            ) as client:
                response = client.post(self._endpoint, json=payload)
        except httpx.HTTPError as error:
            log_event(
                "llm.unavailable",
                task=task,
                reason="transport_error",
                error_type=type(error).__name__,
            )
            raise LLMUnavailable("inference server unreachable") from error

        if not response.is_success:
            # The response body may echo the prompt: only the status code is kept.
            log_event(
                "llm.unavailable",
                task=task,
                reason="http_status",
                status_code=response.status_code,
            )
            raise LLMUnavailable(f"inference server returned HTTP {response.status_code}")

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            log_event("llm.invalid_output", task=task, reason="malformed_completion")
            raise LLMInvalidOutput("malformed completion envelope") from error
        if not isinstance(content, str):
            log_event("llm.invalid_output", task=task, reason="malformed_completion")
            raise LLMInvalidOutput("malformed completion envelope")
        return content

    @staticmethod
    def _parse[T: BaseModel](task: str, content: str, output_model: type[T]) -> T:
        try:
            return output_model.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError) as error:
            log_event("llm.invalid_output", task=task, reason="schema_mismatch")
            raise LLMInvalidOutput("completion does not match the output model") from error


def run_with_attempts[R](
    fn: Callable[[], R], attempts: int, retry_on: tuple[type[Exception], ...]
) -> R:
    """Call ``fn`` up to ``attempts`` times, retrying only on ``retry_on`` exceptions.

    The last retryable exception is re-raised when every attempt fails.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on:
            if attempt == attempts:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def get_llm_client() -> LLMClient:
    """Build the client for the configured private inference server (FastAPI dependency)."""
    settings = get_settings()
    return HttpLLMClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        allowed_hosts=settings.llm_allowed_hosts,
        timeout_seconds=settings.llm_timeout_seconds,
    )
