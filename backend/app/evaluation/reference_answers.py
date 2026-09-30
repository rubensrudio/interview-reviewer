"""Reference answers for unsatisfactory items (CT-46, EVAL-06, EVAL-07, EVAL-15).

The caller builds a reference answer only for answers scored below 3 (EVAL-06); this module
never looks at the score. The model writes the reference text and its essential points from
the question, the skill, the expected level, the reference points and the knowledge-base
sources. The candidate answer is not sent: the reference does not depend on it.

Sources are never taken from the model (EVAL-15): the model returns the URLs it relied on and
only URLs that belong to ``inp.sources`` are kept, mapped back to the input ``SourceRef``.

The model may add an experience example. It must then quote the resume snapshot evidence the
example is based on. Unless that quote is one whole evidence quote of an experience item of
the snapshot (whitespace differences aside) with at least ``_MIN_EVIDENCE_WORDS`` words, the
example is flagged as hypothetical so the report labels it "Hypothetical example" and never
attributes it to the candidate (EVAL-07). Fragments such as a skill name, a title or a single
word never count as evidence. Only the
descriptive fields and the evidence of snapshot items are sent; employers, institutions and
dates are left out.

Every text sent to the model is framed as untrusted content (DA-8) and every text returned is
sanitized (NUL, control characters and lone surrogates) before it reaches the database.
Prompts, snapshot data and model output are never logged; only counts and failure codes are.
"""

import re

from pydantic import BaseModel, ConfigDict

from app.config import get_settings
from app.evaluation.evaluator import EvaluationInput, EvaluationUnavailable
from app.llm.client import LLMClient, LLMInvalidOutput, LLMUnavailable, run_with_attempts
from app.llm.untrusted import UNTRUSTED_RULES, wrap_untrusted
from app.models.knowledge import SourceRef
from app.models.resume import ExtractionItem
from app.observability import log_event

__all__ = ["REFERENCE_ANSWER_TASK", "ReferenceAnswer", "build_reference_answer"]

REFERENCE_ANSWER_TASK = "reference_answer"

# Snapshot fields that describe what the candidate did, without identifying where or when.
_SNAPSHOT_FIELDS = ("title", "name", "degree", "description")

# An example is attributed to the candidate only through a whole experience evidence quote
# of at least this many words.
_MIN_EVIDENCE_WORDS = 4

_WHITESPACE_RE = re.compile(r"\s+")
# NUL is dropped (PostgreSQL text cannot hold it); other C0/C1 controls and lone surrogates
# (which cannot be encoded as UTF-8) become spaces.
_NUL_RE = re.compile("\x00")
_CONTROL_RE = re.compile("[\x01-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")

_NOT_SPECIFIED = "not specified"

_SYSTEM_PROMPT = (
    "You are a senior technical interviewer writing the reference answer to one interview "
    "question about one skill, for a candidate who answered it unsatisfactorily. Write a "
    "correct and complete answer for the expected level, based on the reference points and "
    "the sources.\n"
    'Return JSON with the keys "text", "points", "source_urls", "example_text" and '
    '"example_evidence".\n'
    '- "text" is the reference answer. It must not be empty and must not contain the '
    "experience example.\n"
    '- "points" lists the essential points the answer must cover, one short sentence each.\n'
    '- "source_urls" lists the URLs of the given sources you relied on. Only use URLs of the '
    "given sources; never invent or add other URLs. Use an empty list when no source is "
    "given or none was used.\n"
    '- "example_text" is an optional short experience example that illustrates the answer, '
    "or null.\n"
    '- "example_evidence" is one whole evidence line, copied verbatim from an experience '
    "resume item, of the experience the example is based on, or null when the example is "
    "not based on an experience of the resume items. Never invent, shorten or paraphrase "
    "evidence.\n\n" + UNTRUSTED_RULES
)


class ReferenceAnswer(BaseModel):
    """A validated reference answer, ready to be frozen into the report."""

    model_config = ConfigDict(extra="forbid")

    text: str
    points: list[str]
    sources: list[SourceRef]
    hypothetical_example: bool
    example_text: str | None


class _LLMReferenceAnswer(BaseModel):
    """Answer expected from the model."""

    model_config = ConfigDict(extra="forbid")

    text: str
    points: list[str]
    source_urls: list[str]
    example_text: str | None
    example_evidence: str | None


def _sanitize(text: str) -> str:
    return _CONTROL_RE.sub(" ", _NUL_RE.sub("", text))


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", _sanitize(text)).strip()


def _snapshot_item_prompt(item: ExtractionItem) -> str:
    lines = [f"kind: {item.kind}"]
    for name in _SNAPSHOT_FIELDS:
        value = _normalize(item.fields.get(name, ""))
        if value:
            lines.append(f"{name}: {value}")
    for evidence in item.evidence:
        value = _normalize(evidence)
        if value:
            lines.append(f"evidence: {value}")
    return "\n".join(lines)


def _build_user_prompt(inp: EvaluationInput, snapshot: list[ExtractionItem]) -> str:
    parts = [
        "Write the reference answer for the question below.",
        "Question:\n" + wrap_untrusted("question", _sanitize(inp.question)),
        "Skill:\n" + wrap_untrusted("skill", _sanitize(inp.skill)),
        "Expected level:\n"
        + wrap_untrusted("expected_level", _sanitize(inp.expected_level or _NOT_SPECIFIED)),
    ]
    reference_points = "\n".join(f"- {_sanitize(point)}" for point in inp.reference_points)
    parts.append(
        "Reference points:\n"
        + wrap_untrusted("reference_points", reference_points or _NOT_SPECIFIED)
    )
    if not inp.sources:
        parts.append("Sources: none. Return an empty source_urls list.")
    for index, source in enumerate(inp.sources, start=1):
        parts.append(
            f"Source {index}:\n"
            + wrap_untrusted(
                "source",
                f"{_sanitize(source.url)}\n{_sanitize(source.title)}\n{_sanitize(source.excerpt)}",
            )
        )
    if not snapshot:
        parts.append("Resume items: none. Any example must use a null example_evidence.")
    for index, item in enumerate(snapshot, start=1):
        parts.append(
            f"Resume item {index}:\n" + wrap_untrusted("resume_item", _snapshot_item_prompt(item))
        )
    return "\n\n".join(parts)


def _known_sources(urls: list[str], sources: list[SourceRef]) -> list[SourceRef]:
    """Map the URLs cited by the model back to ``sources``; unknown URLs are dropped."""
    by_url: dict[str, SourceRef] = {}
    for source in sources:
        by_url.setdefault(source.url.strip(), source)
    kept: list[SourceRef] = []
    for url in urls:
        known = by_url.get(_sanitize(url).strip())
        if known is not None and known not in kept:
            kept.append(known)
    return kept


def _is_literal_evidence(evidence: str | None, snapshot: list[ExtractionItem]) -> bool:
    """True only when ``evidence`` is one whole, substantive evidence quote of an experience.

    Fragments (a skill name, a title, a single word, part of a quote) could fit any invented
    story, so they never make an example the candidate's own; in doubt it is hypothetical.
    """
    normalized = _normalize(evidence or "")
    if len(normalized.split(" ")) < _MIN_EVIDENCE_WORDS:
        return False
    return any(
        normalized == _normalize(quote)
        for item in snapshot
        if item.kind == "experience"
        for quote in item.evidence
    )


def build_reference_answer(
    llm: LLMClient, inp: EvaluationInput, snapshot: list[ExtractionItem]
) -> ReferenceAnswer:
    """Build the reference answer of one unsatisfactory item (score < 3, checked by the caller).

    Raises ``EvaluationUnavailable`` when no valid reference answer is produced within
    ``llm_max_attempts`` attempts.
    """
    user_prompt = _build_user_prompt(inp, snapshot)

    def attempt() -> ReferenceAnswer:
        output = llm.complete_structured(
            REFERENCE_ANSWER_TASK, _SYSTEM_PROMPT, user_prompt, _LLMReferenceAnswer
        )
        text = _sanitize(output.text).strip()
        if not text:
            log_event("reference_answer.invalid_output", reason="empty_text")
            raise LLMInvalidOutput("reference answer text is empty")
        points = [point for point in (_sanitize(p).strip() for p in output.points) if point]
        example_text = _sanitize(output.example_text or "").strip() or None
        hypothetical = example_text is not None and not _is_literal_evidence(
            output.example_evidence, snapshot
        )
        return ReferenceAnswer(
            text=text,
            points=points,
            sources=_known_sources(output.source_urls, inp.sources),
            hypothetical_example=hypothetical,
            example_text=example_text,
        )

    attempts = get_settings().llm_max_attempts
    try:
        result = run_with_attempts(
            attempt, attempts=attempts, retry_on=(LLMInvalidOutput, LLMUnavailable)
        )
    except LLMInvalidOutput as error:
        log_event("reference_answer.failed", reason="invalid_output", attempts=attempts)
        raise EvaluationUnavailable("invalid_output") from error
    except LLMUnavailable as error:
        log_event("reference_answer.failed", reason="llm_unavailable", attempts=attempts)
        raise EvaluationUnavailable("llm_unavailable") from error

    log_event(
        "reference_answer.built",
        sources=len(result.sources),
        hypothetical_example=result.hypothetical_example,
    )
    return result
