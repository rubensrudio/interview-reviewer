import io
import json
import logging
from collections.abc import Iterator

import pytest

from app.logging_setup import JsonFormatter, RedactionFilter
from app.observability import log_event, timed


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


def test_timed_emits_event_with_integer_duration(captured: io.StringIO) -> None:
    with timed("llm.inference", task="x"):
        pass

    (line,) = _lines(captured)
    assert line["event"] == "llm.inference"
    assert line["task"] == "x"
    duration = line["duration_ms"]
    assert isinstance(duration, int) and not isinstance(duration, bool)
    assert duration >= 0
    assert line["outcome"] == "ok"


def test_timed_records_error_outcome_and_reraises(captured: io.StringIO) -> None:
    with pytest.raises(RuntimeError), timed("llm.inference", task="x"):
        raise RuntimeError("boom")

    (line,) = _lines(captured)
    assert line["outcome"] == "error"
    assert isinstance(line["duration_ms"], int)


def test_timed_keeps_caller_outcome(captured: io.StringIO) -> None:
    with timed("llm.inference", task="x", outcome="retry"):
        pass

    (line,) = _lines(captured)
    assert line["outcome"] == "retry"


def test_timed_redacts_sensitive_fields(captured: io.StringIO) -> None:
    with timed("llm.inference", task="x", prompt="my resume text"):
        pass

    assert "my resume text" not in captured.getvalue()


def test_log_event_is_reexported(captured: io.StringIO) -> None:
    log_event("auth.login", outcome="success")

    (line,) = _lines(captured)
    assert line == {**line, "event": "auth.login", "outcome": "success"}
