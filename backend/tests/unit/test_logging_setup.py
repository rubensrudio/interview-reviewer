import io
import json
import logging
from collections.abc import Iterator

import pytest

from app.logging_setup import (
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    configure_logging,
    log_event,
)


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield stream
    root.removeHandler(handler)
    root.setLevel(previous_level)


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_auth_95_log_event_drops_blacklisted_fields_and_keeps_ids(
    captured: io.StringIO,
) -> None:
    log_event("x", password="p", token="t", resume_id="1")  # noqa: S106

    raw = captured.getvalue()
    (line,) = _lines(captured)
    assert line["event"] == "x"
    assert line["resume_id"] == "1"
    assert "password" not in line
    assert "token" not in line
    assert '"p"' not in raw
    assert '"t"' not in raw


@pytest.mark.parametrize(
    "key",
    [
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
        "access_token",
        "client_secret",
        "verification_link",
        "user_email",
        "password_hash",
        "Token",
    ],
)
def test_auth_95_sensitive_keys_are_removed(captured: io.StringIO, key: str) -> None:
    log_event("evt", **{key: "sensitive-value"})

    assert "sensitive-value" not in captured.getvalue()
    (line,) = _lines(captured)
    assert key not in line


def test_auth_95_ids_and_counts_are_kept(captured: io.StringIO) -> None:
    log_event("pdf.processed", resume_id="r1", pages=2, text_chars=900, duration_ms=5)

    (line,) = _lines(captured)
    assert line["resume_id"] == "r1"
    assert line["pages"] == 2
    assert line["text_chars"] == 900
    assert line["duration_ms"] == 5


def test_auth_95_token_in_message_is_masked(captured: io.StringIO) -> None:
    logging.getLogger("app.test").info("sent https://h/verify-email?token=abc to user")

    raw = captured.getvalue()
    (line,) = _lines(captured)
    assert "abc" not in raw
    assert "token=[REDACTED]" in str(line["message"])


def test_auth_95_token_in_message_args_is_masked(captured: io.StringIO) -> None:
    logging.getLogger("app.test").info("link %s", "https://h/reset?x=1&token=abc&y=2")

    raw = captured.getvalue()
    assert "abc" not in raw
    (line,) = _lines(captured)
    assert line["message"] == "link https://h/reset?x=1&token=[REDACTED]&y=2"


def test_auth_95_token_in_field_value_is_masked(captured: io.StringIO) -> None:
    log_event("email.send_failed", template="verify", url="https://h/v?token=abc")

    raw = captured.getvalue()
    assert "abc" not in raw
    (line,) = _lines(captured)
    assert line["url"] == f"https://h/v?token={REDACTED}"


def test_log_event_drops_non_scalar_values(captured: io.StringIO) -> None:
    log_event("evt", ok=True, bad={"nested": "secret-data"})  # type: ignore[arg-type]

    raw = captured.getvalue()
    (line,) = _lines(captured)
    assert line["ok"] is True
    assert "bad" not in line
    assert "secret-data" not in raw


def test_log_event_keeps_scalar_types(captured: io.StringIO) -> None:
    log_event("evt", a="s", b=1, c=1.5, d=False, e=None)

    (line,) = _lines(captured)
    assert (line["a"], line["b"], line["c"], line["d"], line["e"]) == ("s", 1, 1.5, False, None)
    assert line["level"] == "INFO"
    assert "timestamp" in line


def test_exception_is_logged_without_stack_trace(captured: io.StringIO) -> None:
    try:
        raise ValueError("token=abc")
    except ValueError:
        logging.getLogger("app.test").exception("failed")

    raw = captured.getvalue()
    assert "Traceback" not in raw
    assert "abc" not in raw
    (line,) = _lines(captured)
    assert line["exc_type"] == "ValueError"


def test_configure_logging_is_idempotent() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    previous_level = root.level
    for handler in before:
        root.removeHandler(handler)
    try:
        configure_logging()
        configure_logging()
        assert len(root.handlers) == 1
        (handler,) = root.handlers
        assert isinstance(handler.formatter, JsonFormatter)
        assert any(isinstance(f, RedactionFilter) for f in handler.filters)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)
        root.setLevel(previous_level)


@pytest.mark.parametrize("key", ["email_to", "user_email_address", "reset_link_url"])
def test_auth_95_regression_email_and_link_segments_anywhere_are_removed(
    captured: io.StringIO, key: str
) -> None:
    log_event("auth.reset", **{key: "leaked-value"})

    assert "leaked-value" not in captured.getvalue()
    (line,) = _lines(captured)
    assert key not in line


def test_auth_95_regression_reset_link_path_and_email_in_message_are_masked(
    captured: io.StringIO,
) -> None:
    logging.getLogger("app.auth").info(
        "reset link sent https://h/reset-password/SECRETPATHTOKEN123 to a@b.com"
    )

    raw = captured.getvalue()
    assert "SECRETPATHTOKEN123" not in raw
    assert "a@b.com" not in raw
    (line,) = _lines(captured)
    assert "https://h/reset-password/[REDACTED]" in str(line["message"])


def test_auth_95_regression_client_secret_in_message_is_masked(captured: io.StringIO) -> None:
    logging.getLogger("app.auth").info("oidc client_secret=GOCSPX-abc123 password=hunter2")

    raw = captured.getvalue()
    assert "GOCSPX-abc123" not in raw
    assert "hunter2" not in raw
    (line,) = _lines(captured)
    assert "client_secret=[REDACTED]" in str(line["message"])


def test_auth_95_regression_email_and_link_in_field_value_are_masked(
    captured: io.StringIO,
) -> None:
    log_event("evt", detail="to a@b.com via https://h/verify-email/ABCSECRET")

    raw = captured.getvalue()
    assert "a@b.com" not in raw
    assert "ABCSECRET" not in raw


def test_ct_4_regression_log_event_cannot_override_log_metadata(captured: io.StringIO) -> None:
    log_event("x", level="FAKE", logger="evil", timestamp="1999", message="m")

    (line,) = _lines(captured)
    assert line["event"] == "x"
    assert line["level"] == "INFO"
    assert line["logger"] == "app.events"
    assert line["timestamp"] != "1999"
    assert "message" not in line
