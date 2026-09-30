"""Unit tests for reference answers of unsatisfactory items (CT-46, EVAL-06, EVAL-07, EVAL-15)."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fakes.fake_llm import FakeLLM

from app.config import get_settings
from app.evaluation.evaluator import EvaluationInput, EvaluationUnavailable
from app.evaluation.reference_answers import (
    REFERENCE_ANSWER_TASK,
    ReferenceAnswer,
    build_reference_answer,
)
from app.llm.client import LLMUnavailable
from app.models.knowledge import SourceRef
from app.models.resume import ExtractionItem

REQUIRED_ENV = {
    "IR_THROTTLE_SECRET": "test-throttle-secret",
    "IR_OIDC_STATE_SECRET": "test-oidc-state-secret",
}

KNOWN_URL = "https://www.postgresql.org/docs/current/indexes.html"
UNKNOWN_URL = "https://example.com/made-up-article"
CANDIDATE_ANSWER = "I think indexes are something about caching tables in memory."
RESUME_EVIDENCE = "Tuned PostgreSQL B-tree indexes for the billing reports"


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("IR_LLM_MAX_ATTEMPTS", "3")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _source(url: str = KNOWN_URL, title: str = "PostgreSQL: Indexes") -> SourceRef:
    return SourceRef(
        url=url,
        title=title,
        collected_at=datetime(2026, 1, 10, tzinfo=UTC),
        excerpt="An index is a specific structure that organizes a reference to your data.",
    )


def _input(sources: list[SourceRef] | None = None) -> EvaluationInput:
    return EvaluationInput(
        question="How does a database index speed up queries, and what does it cost?",
        skill="PostgreSQL",
        expected_level="mid-level",
        reference_points=[
            "An index is an auxiliary structure, usually a B-tree",
            "Indexes cost extra storage and slow down writes",
        ],
        sources=[_source()] if sources is None else sources,
        answer=CANDIDATE_ANSWER,
    )


def _snapshot() -> list[ExtractionItem]:
    return [
        ExtractionItem(
            id="exp-1",
            kind="experience",
            fields={
                "title": "Backend Engineer",
                "organization": "Acme Payments",
                "description": "Owned the billing service.",
            },
            origin="explicit",
            evidence=[RESUME_EVIDENCE],
        ),
        ExtractionItem(
            id="skill-1",
            kind="skill",
            fields={"name": "PostgreSQL"},
            origin="explicit",
            evidence=["PostgreSQL"],
        ),
    ]


def _output(
    *,
    text: str = "An index is an auxiliary B-tree that lets the engine find rows quickly.",
    points: list[str] | None = None,
    source_urls: list[str] | None = None,
    example_text: str | None = None,
    example_evidence: str | None = None,
) -> dict[str, object]:
    return {
        "text": text,
        "points": ["B-tree lookup in log time", "Extra storage and slower writes"]
        if points is None
        else points,
        "source_urls": [KNOWN_URL] if source_urls is None else source_urls,
        "example_text": example_text,
        "example_evidence": example_evidence,
    }


def _llm(*responses: object) -> FakeLLM:
    return FakeLLM({REFERENCE_ANSWER_TASK: list(responses)})


# --- EVAL-06: reference answer with essential points ---------------------------------------


def test_eval06_returns_reference_answer_with_points_and_known_sources() -> None:
    llm = _llm(_output())

    result = build_reference_answer(llm, _input(), _snapshot())

    assert isinstance(result, ReferenceAnswer)
    assert result.text.startswith("An index is an auxiliary B-tree")
    assert result.points == ["B-tree lookup in log time", "Extra storage and slower writes"]
    assert result.sources == [_source()]
    assert result.hypothetical_example is False
    assert result.example_text is None
    assert len(llm.calls_for(REFERENCE_ANSWER_TASK)) == 1


def test_eval06_prompt_frames_inputs_as_untrusted_and_omits_candidate_answer() -> None:
    llm = _llm(_output())

    build_reference_answer(llm, _input(), _snapshot())

    user = llm.calls_for(REFERENCE_ANSWER_TASK)[0].user
    assert "<<<UNTRUSTED:question:" in user
    assert "<<<UNTRUSTED:resume_item:" in user
    assert CANDIDATE_ANSWER not in user
    # Employer names are not needed to pick an example and are left out of the prompt.
    assert "Acme Payments" not in user


def test_eval06_blank_points_are_dropped_and_output_is_sanitized() -> None:
    llm = _llm(_output(text="Index\x00 answer\x07.\ud800", points=["  ", "B-tree\x00", ""]))

    result = build_reference_answer(llm, _input(), _snapshot())

    assert result.text == "Index answer ."
    assert result.points == ["B-tree"]


def test_eval06_empty_text_is_retried_then_accepted() -> None:
    llm = _llm(_output(text="   "), _output())

    result = build_reference_answer(llm, _input(), _snapshot())

    assert result.text.startswith("An index")
    assert len(llm.calls_for(REFERENCE_ANSWER_TASK)) == 2


def test_eval06_llm_unavailable_after_all_attempts_raises() -> None:
    llm = _llm(LLMUnavailable, LLMUnavailable, LLMUnavailable)

    with pytest.raises(EvaluationUnavailable) as error:
        build_reference_answer(llm, _input(), _snapshot())

    assert error.value.reason == "llm_unavailable"


def test_eval06_invalid_output_after_all_attempts_raises() -> None:
    llm = _llm(_output(text=""), _output(text=" "), {"text": 3})

    with pytest.raises(EvaluationUnavailable) as error:
        build_reference_answer(llm, _input(), _snapshot())

    assert error.value.reason == "invalid_output"


# --- EVAL-15: only sources from the knowledge base -----------------------------------------


def test_eval15_unknown_url_is_removed_from_sources() -> None:
    llm = _llm(_output(source_urls=[UNKNOWN_URL, KNOWN_URL]))

    result = build_reference_answer(llm, _input(), _snapshot())

    assert [source.url for source in result.sources] == [KNOWN_URL]
    assert UNKNOWN_URL not in [source.url for source in result.sources]


def test_eval15_only_unknown_urls_gives_no_sources() -> None:
    llm = _llm(_output(source_urls=[UNKNOWN_URL]))

    result = build_reference_answer(llm, _input(), _snapshot())

    assert result.sources == []


def test_eval15_empty_input_sources_gives_empty_sources() -> None:
    llm = _llm(_output(source_urls=[KNOWN_URL, UNKNOWN_URL]))

    result = build_reference_answer(llm, _input(sources=[]), _snapshot())

    assert result.sources == []


def test_eval15_duplicate_urls_are_returned_once_as_the_input_source() -> None:
    llm = _llm(_output(source_urls=[f" {KNOWN_URL} ", KNOWN_URL]))

    result = build_reference_answer(llm, _input(), _snapshot())

    assert result.sources == [_source()]


# --- EVAL-07: hypothetical examples ---------------------------------------------------------


def test_eval07_example_with_evidence_missing_from_snapshot_is_hypothetical() -> None:
    llm = _llm(
        _output(
            example_text="For instance, you could index the orders table by customer id.",
            example_evidence="Led the migration of a 2 TB orders database",
        )
    )

    result = build_reference_answer(llm, _input(), _snapshot())

    assert result.hypothetical_example is True
    assert result.example_text == "For instance, you could index the orders table by customer id."


def test_eval07_example_without_evidence_is_hypothetical() -> None:
    llm = _llm(_output(example_text="Imagine a slow report query.", example_evidence=None))

    result = build_reference_answer(llm, _input(), _snapshot())

    assert result.hypothetical_example is True


def test_eval07_example_with_literal_snapshot_evidence_is_not_hypothetical() -> None:
    llm = _llm(
        _output(
            example_text="Your billing report tuning is a good example of B-tree indexes.",
            example_evidence=RESUME_EVIDENCE,
        )
    )

    result = build_reference_answer(llm, _input(), _snapshot())

    assert result.hypothetical_example is False
    assert result.example_text is not None


def test_eval07_evidence_match_tolerates_whitespace_only() -> None:
    llm = _llm(
        _output(
            example_text="Your billing report work.",
            example_evidence="Tuned  PostgreSQL\nB-tree indexes",
        )
    )

    result = build_reference_answer(llm, _input(), _snapshot())

    assert result.hypothetical_example is False


def test_eval07_empty_snapshot_makes_any_example_hypothetical() -> None:
    llm = _llm(_output(example_text="Your billing work.", example_evidence=RESUME_EVIDENCE))

    result = build_reference_answer(llm, _input(), [])

    assert result.hypothetical_example is True


def test_eval07_blank_example_text_means_no_example() -> None:
    llm = _llm(_output(example_text="   ", example_evidence=UNKNOWN_URL))

    result = build_reference_answer(llm, _input(), _snapshot())

    assert result.example_text is None
    assert result.hypothetical_example is False
