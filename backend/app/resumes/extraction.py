"""LLM resume extraction with a deterministic evidence filter (CT-26, CV-07, CV-08, CV-09, CV-94).

The resume text is sent to the private LLM framed as untrusted content (DA-8). The model
answers with experiences, education and skills, each classified as ``explicit`` or
``inferred`` and backed by literal quotes of the resume. The answer is then validated
without the model: an item is discarded when it has no evidence or when any of its quotes
does not appear literally in the resume text (whitespace-normalized comparison). Instructions
embedded in the resume therefore cannot add items that the text does not support.

Invalid output or an unreachable server after ``llm_max_attempts`` raises ``ExtractionFailed``
with a machine-readable ``reason``; no partial extraction is returned.
"""

import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.config import get_settings
from app.llm.client import LLMClient, LLMInvalidOutput, LLMUnavailable, run_with_attempts
from app.llm.untrusted import UNTRUSTED_RULES, wrap_untrusted
from app.models.resume import ExtractionItem, ExtractionKind
from app.observability import log_event

__all__ = [
    "EXTRACTION_TASK",
    "ExtractionFailed",
    "extract_resume_items",
]

EXTRACTION_TASK = "resume_extraction"
_UNTRUSTED_LABEL = "resume"
_ALLOWED_FIELDS = frozenset(
    {"title", "organization", "start", "end", "degree", "institution", "name", "description"}
)
_WHITESPACE_RE = re.compile(r"\s+")

ExtractionFailureReason = Literal["empty_text", "invalid_output", "llm_unavailable"]

_SYSTEM_PROMPT = (
    "You extract structured data from a candidate resume. Return JSON with a single key "
    '"items": a list of objects with the keys "kind", "fields", "origin" and "evidence".\n'
    '- "kind" is "experience", "education" or "skill".\n'
    '- "fields" is an object of strings. For an experience use "title", "organization", '
    '"start", "end" and optionally "description". For an education entry use "degree", '
    '"institution", "start" and "end". For a skill use "name". Omit unknown values.\n'
    '- "origin" is "explicit" when the resume states the item directly, or "inferred" when '
    "it follows clearly from what the resume describes (for example a skill shown by a "
    "described project).\n"
    '- "evidence" is a list of one or more short quotes copied verbatim from the resume '
    "text that support the item. Never paraphrase, translate or invent a quote.\n"
    "Do not invent experiences, certifications, degrees or technology expertise. Leave out "
    "any item you cannot support with a verbatim quote.\n\n" + UNTRUSTED_RULES
)


class ExtractionFailed(Exception):
    """The resume could not be extracted; ``reason`` is a machine code, never user data."""

    def __init__(self, reason: ExtractionFailureReason) -> None:
        super().__init__(reason)
        self.reason: ExtractionFailureReason = reason


class _LLMExtractionItem(BaseModel):
    """One item as answered by the model; ids and filtering are applied afterwards."""

    model_config = ConfigDict(extra="forbid")

    kind: ExtractionKind
    fields: dict[str, str]
    origin: Literal["explicit", "inferred"]
    evidence: list[str]


class _LLMExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[_LLMExtractionItem]


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _supported_evidence(evidence: list[str], normalized_text: str) -> list[str] | None:
    """Return the normalized quotes, or ``None`` if any is empty or absent from the text."""
    quotes = [_normalize(quote) for quote in evidence]
    if not quotes or any(not quote or quote not in normalized_text for quote in quotes):
        return None
    return quotes


def _clean_fields(fields: dict[str, str]) -> dict[str, str]:
    return {
        key: value.strip()
        for key, value in fields.items()
        if key in _ALLOWED_FIELDS and value.strip()
    }


def extract_resume_items(llm: LLMClient, text: str) -> list[ExtractionItem]:
    """Extract experiences, education and skills from ``text``, keeping only supported items.

    Raises ``ExtractionFailed`` when the text is empty or when the model does not produce a
    valid answer within ``llm_max_attempts`` attempts.
    """
    normalized_text = _normalize(text)
    if not normalized_text:
        log_event("resume.extraction_failed", reason="empty_text")
        raise ExtractionFailed("empty_text")

    user_prompt = "Extract the items from the resume below.\n\n" + wrap_untrusted(
        _UNTRUSTED_LABEL, text
    )
    attempts = get_settings().llm_max_attempts
    try:
        answer = run_with_attempts(
            lambda: llm.complete_structured(
                EXTRACTION_TASK, _SYSTEM_PROMPT, user_prompt, _LLMExtraction
            ),
            attempts=attempts,
            retry_on=(LLMInvalidOutput, LLMUnavailable),
        )
    except LLMInvalidOutput as error:
        log_event("resume.extraction_failed", reason="invalid_output", attempts=attempts)
        raise ExtractionFailed("invalid_output") from error
    except LLMUnavailable as error:
        log_event("resume.extraction_failed", reason="llm_unavailable", attempts=attempts)
        raise ExtractionFailed("llm_unavailable") from error

    items: list[ExtractionItem] = []
    for candidate in answer.items:
        evidence = _supported_evidence(candidate.evidence, normalized_text)
        if evidence is None:
            continue
        items.append(
            ExtractionItem(
                id=uuid.uuid4().hex,
                kind=candidate.kind,
                fields=_clean_fields(candidate.fields),
                origin=candidate.origin,
                evidence=evidence,
            )
        )

    log_event(
        "resume.extraction",
        kept=len(items),
        discarded=len(answer.items) - len(items),
    )
    return items
