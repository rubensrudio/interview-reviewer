"""PDF export of a completed report (CT-49, DA-17; EXPT-01).

`render_report_pdf` turns the frozen `ReportContent` into a PDF with the same sections as the
report screen, in the same order: summary, adherence percentage, disclaimer, performance by
skill, answered items, items to improve (with the reference answer and its sources) and the
requirements that were not evaluated.

Every text in the report comes from the candidate, the job description or the model, so it is
untrusted: it is stripped of control characters and lone surrogates and escaped before it
reaches reportlab's paragraph markup, so it is always rendered as literal text. This module
never logs any text.
"""

import re
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # type: ignore[import-untyped]
from reportlab.lib.units import cm  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    Flowable,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from app.models.assessment import SCORE_MAX
from app.models.knowledge import SourceRef
from app.reports.builder import ReportContent, ReportItem

__all__ = ["render_report_pdf"]

# C0 controls except tab/newline/carriage return, DEL and C1 controls.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_TITLE = "Interview report"


class _Styles:
    def __init__(self) -> None:
        sample = getSampleStyleSheet()
        self.title: ParagraphStyle = sample["Title"]
        self.heading: ParagraphStyle = sample["Heading2"]
        self.subheading: ParagraphStyle = sample["Heading3"]
        self.body: ParagraphStyle = sample["BodyText"]
        self.percentage = ParagraphStyle(
            "Percentage", parent=sample["Heading1"], fontSize=20, leading=24
        )
        self.note = ParagraphStyle("Note", parent=sample["BodyText"], fontSize=9, leading=11)


def render_report_pdf(content: ReportContent) -> bytes:
    """Render the frozen report content as a PDF document and return its bytes."""
    styles = _Styles()
    story: list[Flowable] = [Paragraph(_TITLE, styles.title)]

    story.append(_para(content.summary, styles.body))
    story.append(
        Paragraph(
            f"Adherence to the required skills: {_safe(content.adherence_percentage)}%",
            styles.percentage,
        )
    )
    story.append(_para(content.disclaimer, styles.note))

    story.append(Paragraph("Performance by skill", styles.heading))
    if content.skills:
        story.append(
            _bullets(
                [
                    _para(
                        f"{skill.skill}: average {skill.average} "
                        f"(questions {', '.join(str(p) for p in skill.question_positions)})",
                        styles.body,
                    )
                    for skill in content.skills
                ]
            )
        )
    else:
        story.append(Paragraph("No required skill was evaluated.", styles.body))

    story.append(Paragraph("Questions and answers", styles.heading))
    for item in content.items:
        story.extend(_item_flowables(item, styles))

    positions = set(content.unsatisfactory_items)
    unsatisfactory = [item for item in content.items if item.position in positions]
    story.append(Paragraph("Items to improve", styles.heading))
    if unsatisfactory:
        for item in unsatisfactory:
            story.extend(_improvement_flowables(item, styles))
    else:
        story.append(Paragraph("All answers were satisfactory.", styles.body))

    story.append(Paragraph("Not evaluated", styles.heading))
    story.extend(_non_evaluated_flowables(content, styles))

    story.append(Spacer(1, 0.5 * cm))
    story.append(
        _para(
            f"Model version: {content.model_version}. Rubric version: {content.rubric_version}. "
            f"Completed at: {content.completed_at.isoformat()}.",
            styles.note,
        )
    )

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=_TITLE,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )
    document.build(story)
    return buffer.getvalue()


def _item_flowables(item: ReportItem, styles: _Styles) -> list[Flowable]:
    flowables: list[Flowable] = [
        _para(f"Question {item.position} ({item.skill})", styles.subheading),
        _para(item.question, styles.body),
        _labelled("Your answer", item.answer, styles.body),
        Paragraph(f"<b>Score:</b> {item.score}/{SCORE_MAX}", styles.body),
        _labelled("Justification", item.justification, styles.body),
    ]
    if item.evidence_quotes:
        flowables.append(Paragraph("<b>Evidence:</b>", styles.body))
        flowables.append(_bullets([_para(quote, styles.body) for quote in item.evidence_quotes]))
    return flowables


def _improvement_flowables(item: ReportItem, styles: _Styles) -> list[Flowable]:
    flowables: list[Flowable] = [
        _para(f"Question {item.position} ({item.skill})", styles.subheading),
        _para(item.question, styles.body),
    ]
    if item.gap_explanation:
        flowables.append(_labelled("What was missing", item.gap_explanation, styles.body))
    reference = item.reference_answer
    if reference is not None:
        flowables.append(_labelled("Reference answer", reference.text, styles.body))
        if reference.points:
            flowables.append(_bullets([_para(point, styles.body) for point in reference.points]))
        if reference.example_text:
            label = "Hypothetical example" if reference.hypothetical_example else "Example"
            flowables.append(_labelled(label, reference.example_text, styles.body))
    if item.no_verified_source:
        flowables.append(Paragraph("No verified source.", styles.note))
    elif reference is not None and reference.sources:
        flowables.append(Paragraph("<b>Sources:</b>", styles.body))
        flowables.append(_bullets([_source(source, styles) for source in reference.sources]))
    return flowables


def _non_evaluated_flowables(content: ReportContent, styles: _Styles) -> list[Flowable]:
    groups = (
        ("Desirable skills", content.non_evaluated.nice_to_have),
        ("Non-technical requirements", content.non_evaluated.non_technical),
    )
    flowables: list[Flowable] = []
    for label, values in groups:
        flowables.append(Paragraph(f"<b>{label}:</b>", styles.body))
        if values:
            flowables.append(_bullets([_para(value, styles.body) for value in values]))
        else:
            flowables.append(Paragraph("None.", styles.body))
    return flowables


def _source(source: SourceRef, styles: _Styles) -> Paragraph:
    return _para(
        f"{source.title} - {source.url} (collected {source.collected_at.date().isoformat()})",
        styles.body,
    )


def _labelled(label: str, text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(f"<b>{label}:</b> {_safe(text)}", style)


def _para(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(_safe(text), style)


def _bullets(paragraphs: list[Paragraph]) -> ListFlowable:
    return ListFlowable(
        [ListItem(paragraph) for paragraph in paragraphs], bulletType="bullet", leftIndent=12
    )


def _safe(text: str) -> str:
    """Make untrusted text safe for reportlab's paragraph markup."""
    # Lone surrogates cannot be encoded; replace them instead of failing.
    cleaned = text.encode("utf-8", "replace").decode("utf-8")
    cleaned = _CONTROL_CHARS.sub("", cleaned)
    escaped = escape(cleaned)
    return escaped.replace("\r\n", "<br/>").replace("\n", "<br/>").replace("\r", "<br/>")
