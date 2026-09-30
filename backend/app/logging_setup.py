"""Structured JSON logging with a redaction filter (AUTH-95, DA-11).

Every log line is a single JSON object. The redaction filter removes fields whose
names are sensitive and masks ``token=...`` patterns in messages and field values,
so secrets, links and personal data never reach the log output.
"""

import json
import logging
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

type Scalar = str | int | float | bool | None

REDACTED = "[REDACTED]"
EVENTS_LOGGER_NAME = "app.events"
FIELDS_ATTR = "event_fields"

# Field names that must never be logged (plan section 14).
BLACKLISTED_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "token",
        "link",
        "secret",
        "cookie",
        "email",
        "filename",
        "content",
        "text",
        "prompt",
        "requirements",
        "answer",
        "resume",
    }
)
# Segments that make a field sensitive wherever they appear in its name
# (e.g. ``password_hash``, ``token_value``, ``session_cookie``).
ALWAYS_SENSITIVE_SEGMENTS: frozenset[str] = frozenset({"password", "token", "secret", "cookie"})

_TOKEN_PATTERN = re.compile(r"(token=)[^&\s\"'#]+", re.IGNORECASE)
_KEY_SPLIT = re.compile(r"[_.\-]")

_STANDARD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def is_sensitive_key(key: str) -> bool:
    """Return True when a field name must be dropped from logs."""
    normalized = key.lower()
    if normalized in BLACKLISTED_KEYS:
        return True
    segments = [segment for segment in _KEY_SPLIT.split(normalized) if segment]
    if not segments:
        return False
    if segments[-1] in BLACKLISTED_KEYS:
        return True
    return any(segment in ALWAYS_SENSITIVE_SEGMENTS for segment in segments)


def mask_tokens(value: str) -> str:
    """Mask the value of every ``token=`` occurrence in a string."""
    return _TOKEN_PATTERN.sub(rf"\g<1>{REDACTED}", value)


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _clean_fields(fields: dict[str, object]) -> dict[str, Scalar]:
    cleaned: dict[str, Scalar] = {}
    for key, value in fields.items():
        if is_sensitive_key(key) or not _is_scalar(value):
            continue
        scalar: Scalar = value  # type: ignore[assignment]
        cleaned[key] = mask_tokens(scalar) if isinstance(scalar, str) else scalar
    return cleaned


class RedactionFilter(logging.Filter):
    """Drop sensitive fields and mask tokens before a record is formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            message = str(record.msg)
        record.msg = mask_tokens(message)
        record.args = None

        fields = getattr(record, FIELDS_ATTR, None)
        if isinstance(fields, dict):
            setattr(record, FIELDS_ATTR, _clean_fields(fields))

        for key in [k for k in record.__dict__ if k not in _STANDARD_ATTRS and k != FIELDS_ATTR]:
            value = record.__dict__[key]
            if is_sensitive_key(key) or not _is_scalar(value):
                del record.__dict__[key]
            elif isinstance(value, str):
                record.__dict__[key] = mask_tokens(value)
        return True


class JsonFormatter(logging.Formatter):
    """Format records as one JSON object per line, without stack traces."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
        }
        fields = getattr(record, FIELDS_ATTR, None)
        if isinstance(fields, dict):
            payload.update(fields)
        else:
            payload["message"] = record.getMessage()
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key != FIELDS_ATTR and key not in payload:
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            # Only the exception type: messages and tracebacks may carry personal data.
            payload["exc_type"] = record.exc_info[0].__name__
        return json.dumps(payload, default=str, ensure_ascii=False)


class _JsonHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Marker class so configure_logging can stay idempotent."""


def configure_logging(level: int | str = logging.INFO) -> None:
    """Install the JSON + redaction handler on the root logger (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level)
    if any(isinstance(handler, _JsonHandler) for handler in root.handlers):
        return
    handler = _JsonHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    root.addHandler(handler)


def log_event(event: str, **fields: str | int | float | bool | None) -> None:
    """Log a structured event. Only scalar values are accepted; others are dropped."""
    payload: dict[str, object] = {"event": event, **fields}
    logging.getLogger(EVENTS_LOGGER_NAME).info(event, extra={FIELDS_ATTR: payload})


@contextmanager
def timed(metric: str, **fields: str | int | float | bool | None) -> Iterator[None]:
    """Measure the wrapped block and emit ``metric`` with an integer ``duration_ms``."""
    start = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        duration_ms = max(0, int((time.perf_counter() - start) * 1000))
        event_fields: dict[str, str | int | float | bool | None] = {"outcome": outcome, **fields}
        event_fields["duration_ms"] = duration_ms
        log_event(metric, **event_fields)
