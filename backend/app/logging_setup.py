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
ACCESS_LOGGER_NAME = "uvicorn.access"
# Routes whose query string is dropped from the access log (OIDC state/code,
# verification and reset tokens, provider error details).
AUTH_PATH_PREFIX = "/api/auth"
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
# (e.g. ``password_hash``, ``user_email_address``, ``reset_link_url``).
ALWAYS_SENSITIVE_SEGMENTS: frozenset[str] = frozenset(
    {"password", "token", "secret", "cookie", "email", "link"}
)
# Log metadata keys that event fields can never override.
RESERVED_KEYS: frozenset[str] = frozenset({"timestamp", "level", "logger", "message", "exc_type"})

# ``key=value`` pairs whose value is a credential (``token=``, ``client_secret=``,
# ``password=``, OAuth ``code=``...).
_CREDENTIAL_PAIR = re.compile(
    r"(?<![\w-])((?:[\w.-]*?(?:token|secret|password|passwd|cookie)[\w.-]*|code)=)"
    r"[^&\s\"',;#]+",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(\bbearer\s+)[^\s\"',;]+", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_URL_USERINFO = re.compile(r"^(https?://)[^/@]+@", re.IGNORECASE)
# Path segments that introduce one-time links (verification, reset, sign-in).
_LINK_SEGMENT = re.compile(
    r"verif|reset|confirm|activat|magic|invite|link|token|unsubscribe", re.IGNORECASE
)
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


def _mask_url(match: re.Match[str]) -> str:
    url = _URL_USERINFO.sub(rf"\g<1>{REDACTED}@", match.group(0))
    scheme_end = url.index("://") + 3
    path_start = url.find("/", scheme_end)
    if path_start == -1:
        return url
    split_at = min(
        (i for i in (url.find("?", path_start), url.find("#", path_start)) if i != -1),
        default=len(url),
    )
    segments = url[path_start:split_at].split("/")
    for index, segment in enumerate(segments):
        if _LINK_SEGMENT.search(segment):
            segments[index + 1 :] = [REDACTED if part else part for part in segments[index + 1 :]]
            break
    return url[:path_start] + "/".join(segments) + url[split_at:]


def mask_sensitive(value: str) -> str:
    """Mask one-time link paths, credential pairs, bearer tokens and e-mails in a string."""
    masked = _URL.sub(_mask_url, value)
    masked = _CREDENTIAL_PAIR.sub(rf"\g<1>{REDACTED}", masked)
    masked = _BEARER.sub(rf"\g<1>{REDACTED}", masked)
    return _EMAIL.sub(REDACTED, masked)


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _clean_fields(fields: dict[str, object]) -> dict[str, Scalar]:
    cleaned: dict[str, Scalar] = {}
    for key, value in fields.items():
        if key in RESERVED_KEYS or is_sensitive_key(key) or not _is_scalar(value):
            continue
        scalar: Scalar = value  # type: ignore[assignment]
        cleaned[key] = mask_sensitive(scalar) if isinstance(scalar, str) else scalar
    return cleaned


class RedactionFilter(logging.Filter):
    """Drop sensitive fields and mask tokens before a record is formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            message = str(record.msg)
        record.msg = mask_sensitive(message)
        record.args = None

        fields = getattr(record, FIELDS_ATTR, None)
        if isinstance(fields, dict):
            setattr(record, FIELDS_ATTR, _clean_fields(fields))

        for key in [k for k in record.__dict__ if k not in _STANDARD_ATTRS and k != FIELDS_ATTR]:
            value = record.__dict__[key]
            if is_sensitive_key(key) or not _is_scalar(value):
                del record.__dict__[key]
            elif isinstance(value, str):
                record.__dict__[key] = mask_sensitive(value)
        return True


def redact_access_path(full_path: str) -> str:
    """Drop the query string of auth routes and mask credentials elsewhere."""
    path, separator, _query = full_path.partition("?")
    if path == AUTH_PATH_PREFIX or path.startswith(AUTH_PATH_PREFIX + "/"):
        return path
    return mask_sensitive(full_path) if separator else full_path


class AccessLogRedactionFilter(logging.Filter):
    """Redact the request path of ``uvicorn.access`` records (AUTH-95, LAC-37).

    uvicorn logs ``(client_addr, method, full_path, http_version, status_code)``
    as ``record.args`` and its ``AccessFormatter`` unpacks them, so the path is
    rewritten in place and the record shape is kept.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (*args[:2], redact_access_path(args[2]), *args[3:])
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
            payload.update({k: v for k, v in fields.items() if k not in RESERVED_KEYS})
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
    """Install the JSON + redaction handler on the root logger (idempotent).

    Also attaches the access-log filter to the ``uvicorn.access`` logger. A logger
    filter applies whatever handlers uvicorn's ``log_config`` installs and is kept
    by ``logging.config.dictConfig`` when it reconfigures that logger.
    """
    access_logger = logging.getLogger(ACCESS_LOGGER_NAME)
    if not any(isinstance(f, AccessLogRedactionFilter) for f in access_logger.filters):
        access_logger.addFilter(AccessLogRedactionFilter())
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
    payload: dict[str, object] = {"event": event}
    payload.update((key, value) for key, value in fields.items() if key not in RESERVED_KEYS)
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
        if outcome == "error":
            # A failure must never be reported with the caller's success outcome.
            event_fields["outcome"] = outcome
        event_fields["duration_ms"] = duration_ms
        log_event(metric, **event_fields)
