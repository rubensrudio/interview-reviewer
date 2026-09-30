"""Frozen report content (CT-47, plan 7.6).

`build_report_content` assembles everything the completed report shows from the session, its
questions, answers and evaluations and the deterministic adherence result (EVAL-08). It never
calls the LLM: the general summary is a fixed template filled with counts (PR-6).

The content is frozen when the report is stored: sources are full copies (URL, title,
collection date and excerpt), never references to knowledge-base rows, so later updates of the
knowledge base or dead URLs do not change a completed report (KNOW-08, KNOW-91). A question
marked "no verified source" never cites a source (EVAL-15). Desirable skills and non-technical
requirements are listed as not evaluated and never enter the percentage (EVAL-05, PLAN-06).

This module does not write to the database (TASK-054 does) and never logs any text.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.evaluation.reference_answers import ReferenceAnswer
from app.evaluation.scoring import AdherenceResult
from app.llm.model_version import ModelVersion
from app.models.assessment import Answer, Evaluation
from app.models.interview import InterviewSession, Question, RequirementItem
from app.models.knowledge import SourceRef

__all__ = [
    "REPORT_DISCLAIMER",
    "SATISFACTORY_MIN_SCORE",
    "NonEvaluated",
    "ReportContent",
    "ReportItem",
    "ReportPlan",
    "SkillPerformance",
    "build_report_content",
]

# Spec section 9, "Aviso do relatório".
REPORT_DISCLAIMER = (
    "This percentage reflects your answers in this session only. It is not a hiring "
    "prediction or a certification of professional competence."
)

# Scores below this value are unsatisfactory (spec glossary).
SATISFACTORY_MIN_SCORE = 3

_ONE_PLACE = Decimal("0.1")


class SkillPerformance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    # Average of the skill's scores, rounded half-up to one decimal place (e.g. "2.5").
    average: str
    question_positions: list[int]


class ReportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int
    skill: str
    question: str
    answer: str
    score: int
    justification: str
    evidence_quotes: list[str]
    satisfactory: bool
    gap_explanation: str | None
    reference_answer: ReferenceAnswer | None
    no_verified_source: bool


class NonEvaluated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nice_to_have: list[str]
    non_technical: list[str]


class ReportPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planned_count: int
    skills: list[str]


class ReportContent(BaseModel):
    """Frozen report content, stored as JSONB in `reports.content`."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    # One decimal place, e.g. "62.5".
    adherence_percentage: str
    disclaimer: str
    skills: list[SkillPerformance]
    items: list[ReportItem]
    unsatisfactory_items: list[int]
    non_evaluated: NonEvaluated
    plan: ReportPlan
    model_version: str
    rubric_version: str
    sources_used: list[SourceRef]
    completed_at: datetime


def build_report_content(
    session: InterviewSession,
    questions: list[Question],
    answers: list[Answer],
    evaluations: list[Evaluation],
    adherence: AdherenceResult,
    model_version: ModelVersion,
    rubric_version: str,
) -> ReportContent:
    """Assemble the frozen report content; raise `ValueError` on inconsistent input."""
    ordered = sorted(questions, key=lambda question: question.position)
    answer_by_question = {answer.question_id: answer for answer in answers}
    evaluation_by_answer = {evaluation.answer_id: evaluation for evaluation in evaluations}

    items: list[ReportItem] = []
    sources_used: dict[str, SourceRef] = {}
    for question in ordered:
        answer = answer_by_question.get(question.id)
        if answer is None:
            raise ValueError(f"question at position {question.position} has no answer")
        evaluation = evaluation_by_answer.get(answer.id)
        if evaluation is None:
            raise ValueError(f"question at position {question.position} has no evaluation")
        item = _build_item(question, answer, evaluation)
        items.append(item)
        if not question.no_verified_source:
            _collect(sources_used, (SourceRef.model_validate(raw) for raw in question.sources))
        if item.reference_answer is not None:
            _collect(sources_used, item.reference_answer.sources)

    unsatisfactory = [item.position for item in items if not item.satisfactory]
    percentage = _one_place(adherence.percentage)

    return ReportContent(
        summary=_summary(len(items), len(unsatisfactory), percentage),
        adherence_percentage=percentage,
        disclaimer=REPORT_DISCLAIMER,
        skills=_skills(adherence, items),
        items=items,
        unsatisfactory_items=unsatisfactory,
        non_evaluated=NonEvaluated(
            nice_to_have=_nice_to_have(session),
            non_technical=[str(requirement) for requirement in session.non_technical],
        ),
        plan=_plan(session, ordered),
        model_version=model_version.id,
        rubric_version=rubric_version,
        sources_used=list(sources_used.values()),
        completed_at=session.completed_at or datetime.now(UTC),
    )


def _build_item(question: Question, answer: Answer, evaluation: Evaluation) -> ReportItem:
    satisfactory = evaluation.score >= SATISFACTORY_MIN_SCORE
    reference: ReferenceAnswer | None = None
    # Reference answers only exist for unsatisfactory items (EVAL-06).
    if not satisfactory and evaluation.reference_answer is not None:
        reference = ReferenceAnswer.model_validate(evaluation.reference_answer)
        if question.no_verified_source:
            # Never cite a source for a question without a verified one (EVAL-15).
            reference = reference.model_copy(update={"sources": []})
    return ReportItem(
        position=question.position,
        skill=question.skill_name,
        question=question.text,
        answer=answer.content,
        score=evaluation.score,
        justification=evaluation.justification,
        evidence_quotes=list(evaluation.evidence_quotes),
        satisfactory=satisfactory,
        gap_explanation=None if satisfactory else evaluation.gap_explanation,
        reference_answer=reference,
        no_verified_source=question.no_verified_source,
    )


def _collect(target: dict[str, SourceRef], sources: Iterable[SourceRef]) -> None:
    for source in sources:
        target.setdefault(source.url, source.model_copy())


def _one_place(value: Decimal) -> str:
    return str(value.quantize(_ONE_PLACE, rounding=ROUND_HALF_UP))


def _skills(adherence: AdherenceResult, items: list[ReportItem]) -> list[SkillPerformance]:
    # Only mandatory skills are in the adherence result; desirable ones never appear here.
    return [
        SkillPerformance(
            skill=skill,
            average=_one_place(average),
            question_positions=[item.position for item in items if item.skill == skill],
        )
        for skill, average in adherence.skill_averages.items()
    ]


def _nice_to_have(session: InterviewSession) -> list[str]:
    requirements = [RequirementItem.model_validate(raw) for raw in session.requirement_items]
    return [item.name for item in requirements if item.classification == "nice_to_have"]


def _plan(session: InterviewSession, questions: list[Question]) -> ReportPlan:
    proposal: dict[str, Any] = session.proposal or {}
    skills = proposal.get("skills")
    if not isinstance(skills, list) or not all(isinstance(skill, str) for skill in skills):
        skills = list(dict.fromkeys(question.skill_name for question in questions))
    planned_count = session.planned_count
    if planned_count is None:
        planned_count = len(questions)
    return ReportPlan(planned_count=planned_count, skills=list(skills))


def _summary(answered: int, unsatisfactory: int, percentage: str) -> str:
    satisfactory = answered - unsatisfactory
    return (
        f"You answered {answered} {_plural(answered, 'question', 'questions')}. "
        f"Adherence to the required skills: {percentage}%. "
        f"{satisfactory} {_plural(satisfactory, 'answer was', 'answers were')} satisfactory "
        f"and {unsatisfactory} {_plural(unsatisfactory, 'answer needs', 'answers need')} "
        "improvement."
    )


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural
