"""Clarification requests about the current interview question (CT-42, INTV-08, INTV-92, INTV-93).

A clarification is only accepted while the session is ``in_interview``: terminal sessions are
rejected with ``SESSION_CLOSED`` and any other state with ``INVALID_STATE``. It never counts as
an answer, never changes ``answered_count`` nor the current question, and never asks a new
evaluative question.

The prompt carries only the current question text and the candidate's doubt, both framed as
untrusted content (DA-8). Reference points are never sent. The reply is still checked
deterministically: when it contains at least half of the distinct words of any reference
point of the session, it is replaced by a generic refusal.

The candidate message (``clarification_request``) is flushed before the model is called. When
the model is unavailable or keeps answering invalid output, ``CLARIFICATION_UNAVAILABLE`` (503)
is raised and nothing else is written, so answering keeps working (INTV-92). Prompts, doubts
and replies are never logged. Nothing here commits: the caller does.
"""

import re

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import (
    CLARIFICATION_UNAVAILABLE,
    INVALID_STATE,
    SESSION_CLOSED,
    VALIDATION_ERROR,
    AppError,
)
from app.interviews.sessions import touch_activity
from app.interviews.state_machine import TERMINAL_STATUSES
from app.llm.client import LLMClient, LLMInvalidOutput, LLMUnavailable, run_with_attempts
from app.llm.untrusted import UNTRUSTED_RULES, wrap_untrusted
from app.models.assessment import Answer
from app.models.interview import (
    InterviewSession,
    Message,
    MessageKind,
    MessageRole,
    Question,
    SessionStatus,
)
from app.observability import log_event

__all__ = ["CLARIFICATION_REFUSAL", "CLARIFICATION_TASK", "request_clarification"]

CLARIFICATION_TASK = "clarification"

# Shown instead of a reply that would reveal the expected answer (INTV-08).
CLARIFICATION_REFUSAL = (
    "I can't give more details about this question without revealing the expected answer. "
    "Please answer based on your own understanding of it."
)

_LLM_ATTEMPTS = 2
_LEAK_RATIO = 0.5

# NUL is dropped (PostgreSQL text cannot hold it); other C0/C1 controls and lone surrogates
# (which cannot be encoded as UTF-8) become spaces.
_NUL_RE = re.compile("\x00")
_CONTROL_RE = re.compile("[\x01-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
_WORD_RE = re.compile(r"\w+")

_SYSTEM_PROMPT = (
    "You are a technical interviewer. The candidate asked for a clarification about the "
    "current interview question. Explain what the question asks, its scope or any ambiguous "
    "term, in at most a few sentences.\n"
    "- Never answer the question, never hint at the expected answer and never list what a "
    "good answer should contain.\n"
    "- Never ask a new question and never change the question.\n"
    "- If the clarification cannot be given without revealing the answer, say so politely.\n"
    'Return JSON with the single key "reply" holding your message to the candidate.\n\n'
    + UNTRUSTED_RULES
)


class _ClarificationOutput(BaseModel):
    """Answer expected from the model."""

    model_config = ConfigDict(extra="forbid")

    reply: str


def _sanitize(text: str) -> str:
    return _CONTROL_RE.sub(" ", _NUL_RE.sub("", text))


def _invalid_text(reason: str) -> AppError:
    return AppError.from_catalog(VALIDATION_ERROR, {"fields": [{"loc": ["text"], "type": reason}]})


def _validated_text(text: object) -> str:
    if not isinstance(text, str):
        raise _invalid_text("string_type")
    cleaned = _sanitize(text).strip()
    if not cleaned:
        raise _invalid_text("missing")
    if len(cleaned) > get_settings().max_answer_chars:
        raise _invalid_text("string_too_long")
    return cleaned


def _ensure_in_interview(session: InterviewSession) -> None:
    if session.status in TERMINAL_STATUSES:
        raise AppError.from_catalog(SESSION_CLOSED)
    if session.status != SessionStatus.IN_INTERVIEW:
        raise AppError.from_catalog(INVALID_STATE)


def _current_question(db: Session, session: InterviewSession) -> tuple[Question, list[str]]:
    """Return the first unanswered question and the reference points of the whole session."""
    questions = list(
        db.execute(
            select(Question).where(Question.session_id == session.id).order_by(Question.position)
        )
        .scalars()
        .all()
    )
    answered = set(
        db.execute(select(Answer.question_id).where(Answer.session_id == session.id))
        .scalars()
        .all()
    )
    current = next((q for q in questions if q.id not in answered), None)
    if current is None:
        raise AppError.from_catalog(INVALID_STATE)
    points = [str(point) for question in questions for point in question.reference_points]
    return current, points


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.casefold()))


def _leaks_reference_point(reply: str, reference_points: list[str]) -> bool:
    reply_words = _words(reply)
    for point in reference_points:
        point_words = _words(point)
        if point_words and len(point_words & reply_words) / len(point_words) >= _LEAK_RATIO:
            return True
    return False


def _build_user_prompt(question_text: str, doubt: str) -> str:
    return "\n\n".join(
        [
            "Current interview question:\n" + wrap_untrusted("question", _sanitize(question_text)),
            "Candidate clarification request:\n" + wrap_untrusted("clarification_request", doubt),
        ]
    )


def _ask_model(llm: LLMClient, user_prompt: str) -> str:
    def attempt() -> str:
        output = llm.complete_structured(
            CLARIFICATION_TASK, _SYSTEM_PROMPT, user_prompt, _ClarificationOutput
        )
        reply = _sanitize(output.reply).strip()
        if not reply:
            raise LLMInvalidOutput("empty clarification reply")
        return reply

    return run_with_attempts(attempt, _LLM_ATTEMPTS, (LLMInvalidOutput,))


def request_clarification(
    db: Session, llm: LLMClient, session: InterviewSession, text: str
) -> Message:
    """Answer a clarification about the current question and return the assistant message.

    Raises ``AppError`` ``SESSION_CLOSED`` (terminal session), ``INVALID_STATE`` (any other
    state than ``in_interview`` or no question left), ``VALIDATION_ERROR`` (empty or longer
    than ``max_answer_chars``) or ``CLARIFICATION_UNAVAILABLE`` (model unavailable; only the
    candidate message was added). The caller commits.
    """
    _ensure_in_interview(session)
    doubt = _validated_text(text)
    question, reference_points = _current_question(db, session)

    touch_activity(session)
    db.add(
        Message(
            session_id=session.id,
            role=MessageRole.CANDIDATE,
            kind=MessageKind.CLARIFICATION_REQUEST,
            question_id=question.id,
            content=doubt,
        )
    )
    db.flush()

    try:
        reply = _ask_model(llm, _build_user_prompt(question.text, doubt))
    except (LLMUnavailable, LLMInvalidOutput) as error:
        log_event(
            "clarification.unavailable",
            session_id=str(session.id),
            error_type=type(error).__name__,
        )
        raise AppError.from_catalog(CLARIFICATION_UNAVAILABLE) from None

    refused = _leaks_reference_point(reply, reference_points)
    message = Message(
        session_id=session.id,
        role=MessageRole.ASSISTANT,
        kind=MessageKind.CLARIFICATION_REPLY,
        question_id=question.id,
        content=CLARIFICATION_REFUSAL if refused else reply,
    )
    db.add(message)
    db.flush()
    db.refresh(message)
    log_event("clarification.answered", session_id=str(session.id), refused=refused)
    return message
