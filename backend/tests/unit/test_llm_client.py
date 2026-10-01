"""Unit tests for the private LLM client (CT-19, KNOW-01, KNOW-92) and the FakeLLM."""

import io
import json
import logging
from collections.abc import Callable, Iterator

import httpx
import pytest
from fakes.fake_llm import FakeLLM
from pydantic import BaseModel

from app.config import get_settings
from app.llm.client import (
    HttpLLMClient,
    LLMInvalidOutput,
    LLMUnavailable,
    get_llm_client,
    run_with_attempts,
)
from app.logging_setup import JsonFormatter, RedactionFilter

REQUIRED_ENV = {
    "IR_THROTTLE_SECRET": "test-throttle-secret",
    "IR_OIDC_STATE_SECRET": "test-oidc-state-secret",
}
BASE_URL = "http://llm.internal:8000/v1"
PROMPT_MARKER = "PRIVATE-PROMPT-MARKER"
PRIVATE_PROMPT = f"candidate resume says {PROMPT_MARKER}"


class Skill(BaseModel):
    name: str
    level: int


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


def _completion(content: str) -> dict[str, object]:
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> HttpLLMClient:
    return HttpLLMClient(
        base_url=BASE_URL,
        model="test-model",
        allowed_hosts=["llm.internal"],
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


# --- Host allowlist (KNOW-01) ---------------------------------------------------------------


def test_know_01_external_host_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        HttpLLMClient(
            base_url="https://api.openai.com/v1",
            model="gpt",
            allowed_hosts=["llm.internal"],
            timeout_seconds=5,
        )


def test_know_01_get_llm_client_refuses_host_outside_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IR_LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("IR_LLM_ALLOWED_HOSTS", '["llm.internal"]')
    get_settings.cache_clear()

    with pytest.raises(ValueError):
        get_llm_client()


def test_know_01_get_llm_client_accepts_allowed_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IR_LLM_BASE_URL", BASE_URL)
    monkeypatch.setenv("IR_LLM_ALLOWED_HOSTS", '["LLM.internal"]')
    get_settings.cache_clear()

    assert isinstance(get_llm_client(), HttpLLMClient)


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://llm.internal/v1",
        "llm.internal/v1",
        "http://llm.internal.evil.com/v1",
        "http://evil.com/llm.internal",
        "http://llm.internal@evil.com/v1",
        "",
    ],
)
def test_know_01_malformed_or_disguised_urls_are_refused(base_url: str) -> None:
    with pytest.raises(ValueError):
        HttpLLMClient(
            base_url=base_url, model="m", allowed_hosts=["llm.internal"], timeout_seconds=5
        )


# --- complete_structured --------------------------------------------------------------------


def test_valid_json_response_returns_output_model_instance() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion('{"name": "Python", "level": 3}'))

    result = _client(handler).complete_structured(
        "resume_extraction", "system rules", "user text", Skill
    )

    assert result == Skill(name="Python", level=3)
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "http://llm.internal:8000/v1/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "test-model"
    assert body["messages"] == [
        {"role": "system", "content": "system rules"},
        {"role": "user", "content": "user text"},
    ]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == Skill.model_json_schema()


def test_know_92_503_raises_llm_unavailable() -> None:
    client = _client(lambda request: httpx.Response(503, text="overloaded"))

    with pytest.raises(LLMUnavailable):
        client.complete_structured("resume_extraction", "s", "u", Skill)


def test_know_92_timeout_raises_llm_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(LLMUnavailable):
        _client(handler).complete_structured("resume_extraction", "s", "u", Skill)


def test_know_92_connection_error_raises_llm_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMUnavailable):
        _client(handler).complete_structured("resume_extraction", "s", "u", Skill)


def test_json_outside_schema_raises_llm_invalid_output() -> None:
    client = _client(lambda request: httpx.Response(200, json=_completion('{"name": "Python"}')))

    with pytest.raises(LLMInvalidOutput):
        client.complete_structured("resume_extraction", "s", "u", Skill)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json=_completion("not json at all")),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, text="<html>not json</html>"),
    ],
)
def test_malformed_completion_raises_llm_invalid_output(response: httpx.Response) -> None:
    client = _client(lambda request: response)

    with pytest.raises(LLMInvalidOutput):
        client.complete_structured("resume_extraction", "s", "u", Skill)


def test_invalid_task_name_is_rejected() -> None:
    client = _client(lambda request: httpx.Response(200, json=_completion("{}")))

    with pytest.raises(ValueError):
        client.complete_structured("Bad Task!", "s", "u", Skill)


def test_inference_is_timed_without_logging_the_prompt(captured: io.StringIO) -> None:
    client = _client(
        lambda request: httpx.Response(200, json=_completion('{"name": "Go", "level": 1}'))
    )

    client.complete_structured("resume_extraction", PRIVATE_PROMPT, PRIVATE_PROMPT, Skill)

    lines = _lines(captured)
    inference = [line for line in lines if line.get("event") == "llm.inference"]
    assert len(inference) == 1
    assert inference[0]["task"] == "resume_extraction"
    assert inference[0]["outcome"] == "ok"
    assert PROMPT_MARKER not in captured.getvalue()


def test_failure_log_has_no_prompt_or_response_body(captured: io.StringIO) -> None:
    client = _client(lambda request: httpx.Response(503, text=f"echo {PROMPT_MARKER}"))

    with pytest.raises(LLMUnavailable) as raised:
        client.complete_structured("resume_extraction", PRIVATE_PROMPT, PRIVATE_PROMPT, Skill)

    assert PROMPT_MARKER not in str(raised.value)
    assert PROMPT_MARKER not in captured.getvalue()
    events = {line.get("event") for line in _lines(captured)}
    assert "llm.unavailable" in events


# --- run_with_attempts ----------------------------------------------------------------------


def test_run_with_attempts_calls_exactly_attempts_times_when_all_fail() -> None:
    calls = 0

    def always_fails() -> Skill:
        nonlocal calls
        calls += 1
        raise LLMUnavailable("down")

    with pytest.raises(LLMUnavailable):
        run_with_attempts(always_fails, 3, (LLMUnavailable, LLMInvalidOutput))

    assert calls == 3


def test_run_with_attempts_returns_first_success() -> None:
    outcomes: list[object] = [LLMInvalidOutput("bad"), Skill(name="Go", level=2)]
    calls = 0

    def flaky() -> Skill:
        nonlocal calls
        calls += 1
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, Skill)
        return outcome

    assert run_with_attempts(flaky, 3, (LLMInvalidOutput,)) == Skill(name="Go", level=2)
    assert calls == 2


def test_run_with_attempts_does_not_retry_other_exceptions() -> None:
    calls = 0

    def boom() -> Skill:
        nonlocal calls
        calls += 1
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        run_with_attempts(boom, 3, (LLMUnavailable,))

    assert calls == 1


def test_run_with_attempts_requires_at_least_one_attempt() -> None:
    with pytest.raises(ValueError):
        run_with_attempts(lambda: Skill(name="Go", level=1), 0, (LLMUnavailable,))


# --- FakeLLM --------------------------------------------------------------------------------


def test_fake_llm_returns_scripted_responses_in_order_per_task() -> None:
    fake = FakeLLM(
        {
            "resume_extraction": [
                {"name": "Python", "level": 3},
                '{"name": "Go", "level": 1}',
                Skill(name="Rust", level=2),
            ],
            "clarification": [LLMUnavailable("down")],
        }
    )

    assert fake.complete_structured("resume_extraction", "s", "u1", Skill).name == "Python"
    assert fake.complete_structured("resume_extraction", "s", "u2", Skill).name == "Go"
    assert fake.complete_structured("resume_extraction", "s", "u3", Skill).name == "Rust"
    with pytest.raises(LLMUnavailable):
        fake.complete_structured("clarification", "s", "u", Skill)

    assert [call.user for call in fake.calls_for("resume_extraction")] == ["u1", "u2", "u3"]
    assert fake.remaining("resume_extraction") == 0


def test_fake_llm_invalid_response_raises_llm_invalid_output() -> None:
    fake = FakeLLM({"answer_evaluation": [{"name": "Python"}, LLMInvalidOutput]})

    with pytest.raises(LLMInvalidOutput):
        fake.complete_structured("answer_evaluation", "s", "u", Skill)
    with pytest.raises(LLMInvalidOutput):
        fake.complete_structured("answer_evaluation", "s", "u", Skill)


def test_fake_llm_fails_loudly_when_script_is_exhausted() -> None:
    fake = FakeLLM({})

    with pytest.raises(AssertionError):
        fake.complete_structured("question_generation", "s", "u", Skill)
