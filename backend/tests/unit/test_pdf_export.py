"""Unit tests for the report PDF export (TASK-059, CT-49; EXPT-01)."""

from datetime import UTC, datetime
from io import BytesIO

import pytest
from pypdf import PdfReader

from app.evaluation.reference_answers import ReferenceAnswer
from app.models.knowledge import SourceRef
from app.reports.builder import (
    REPORT_DISCLAIMER,
    NonEvaluated,
    ReportContent,
    ReportItem,
    ReportPlan,
    SkillPerformance,
)
from app.reports.pdf_export import render_report_pdf

SOURCE = SourceRef(
    url="https://docs.python.org/3/glossary.html",
    title="Python glossary",
    collected_at=datetime(2026, 1, 10, tzinfo=UTC),
    excerpt="The global interpreter lock.",
)


def _item(position: int, question: str, score: int, **overrides: object) -> ReportItem:
    satisfactory = score >= 3
    data: dict[str, object] = {
        "position": position,
        "skill": "Python",
        "question": question,
        "answer": f"Answer number {position}.",
        "score": score,
        "justification": f"Justification {position}.",
        "evidence_quotes": [f"Quote {position}"],
        "satisfactory": satisfactory,
        "gap_explanation": None if satisfactory else "Missing the core idea.",
        "reference_answer": None
        if satisfactory
        else ReferenceAnswer(
            text="The GIL serialises bytecode execution.",
            points=["Only one thread runs bytecode at a time"],
            sources=[SOURCE],
            hypothetical_example=False,
            example_text=None,
        ),
        "no_verified_source": False,
    }
    data.update(overrides)
    return ReportItem.model_validate(data)


def _content(items: list[ReportItem]) -> ReportContent:
    return ReportContent(
        summary="You answered 2 questions.",
        adherence_percentage="62.5",
        disclaimer=REPORT_DISCLAIMER,
        skills=[SkillPerformance(skill="Python", average="2.5", question_positions=[1, 2])],
        items=items,
        unsatisfactory_items=[item.position for item in items if not item.satisfactory],
        non_evaluated=NonEvaluated(nice_to_have=["Kubernetes"], non_technical=["Teamwork"]),
        plan=ReportPlan(planned_count=len(items), skills=["Python"]),
        model_version="model-v1",
        rubric_version="rubric-v1",
        sources_used=[SOURCE],
        completed_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


def _text(pdf: bytes) -> str:
    reader = PdfReader(BytesIO(pdf))
    raw = " ".join(page.extract_text() or "" for page in reader.pages)
    return " ".join(raw.split())


def _flat(value: str) -> str:
    return " ".join(value.split())


def test_expt01_pdf_contains_percentage_disclaimer_and_every_question() -> None:
    content = _content(
        [
            _item(1, "What does the GIL protect?", 4),
            _item(2, "How do you profile a slow function?", 1),
        ]
    )

    pdf = render_report_pdf(content)

    assert pdf.startswith(b"%PDF")
    text = _text(pdf)
    assert "62.5%" in text
    assert _flat(REPORT_DISCLAIMER) in text
    for item in content.items:
        assert _flat(item.question) in text


def test_expt01_pdf_keeps_screen_section_order() -> None:
    content = _content(
        [
            _item(1, "What does the GIL protect?", 4),
            _item(2, "How do you profile a slow function?", 1),
        ]
    )

    text = _text(render_report_pdf(content))

    markers = [
        "You answered 2 questions.",
        "62.5%",
        _flat(REPORT_DISCLAIMER),
        "Performance by skill",
        "What does the GIL protect?",
        "Items to improve",
        "The GIL serialises bytecode execution.",
        "https://docs.python.org/3/glossary.html",
        "Not evaluated",
        "Kubernetes",
        "Teamwork",
    ]
    positions = [text.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_expt01_no_verified_source_item_has_no_source_listed() -> None:
    item = _item(
        1,
        "Explain the event loop.",
        1,
        no_verified_source=True,
        reference_answer=ReferenceAnswer(
            text="It schedules callbacks.",
            points=[],
            sources=[],
            hypothetical_example=True,
            example_text="Hypothetical example text.",
        ),
    )
    content = _content([item]).model_copy(update={"sources_used": []})

    text = _text(render_report_pdf(content))

    assert "No verified source" in text
    assert "Hypothetical example text." in text
    assert SOURCE.url not in text


@pytest.mark.parametrize(
    "hostile",
    [
        "<b>not markup</b> & <unclosed",
        "nul\x00byte and \x07bell",
        "lone surrogate \ud800 here",
        "accents çãé and CJK 日本語",
        "x" * 5000,
    ],
)
def test_expt01_untrusted_text_never_breaks_rendering(hostile: str) -> None:
    content = _content([_item(1, hostile, 1, answer=hostile, justification=hostile)])

    pdf = render_report_pdf(content)

    assert pdf.startswith(b"%PDF")
    assert len(PdfReader(BytesIO(pdf)).pages) >= 1


def test_expt01_markup_is_rendered_as_literal_text() -> None:
    content = _content([_item(1, "Is <b>bold</b> & safe?", 4)])

    text = _text(render_report_pdf(content))

    assert "Is <b>bold</b> & safe?" in text
