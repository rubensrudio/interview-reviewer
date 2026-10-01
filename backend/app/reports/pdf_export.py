"""PDF export of a completed report (CT-49, DA-17; EXPT-01, LAC-50).

`render_report_pdf` turns the frozen `ReportContent` into a PDF with the same content, labels and
section order as the report page (`frontend/src/app/features/reports/report-page.html`): summary
(percentage, completion date, summary text), disclaimer, performance by skill, questions and
answers, satisfactory points, unsatisfactory items (with the reference answer and its sources),
the sources used and the requirements that were not evaluated. Model and rubric versions go in
the footer. Values are only formatted, never recalculated.

Every text in the report comes from the candidate, the job description or the model, so it is
untrusted: it is stripped of control characters and lone surrogates and escaped before it
reaches reportlab's paragraph markup, so it is always rendered as literal text. This module
never logs any text.
"""

import re
from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors  # type: ignore[import-untyped]
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
    Table,
    TableStyle,
)

from app.models.knowledge import SourceRef
from app.reports.builder import ReportContent, ReportItem

__all__ = ["render_report_pdf"]

# C0 controls except tab/newline/carriage return, DEL and C1 controls.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# English month names, independent of the system locale (same output as Angular's 'longDate').
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# Labels shared with the report page (report-page.html / report-page.ts).
_TITLE = "Interview report"
_NO_VERIFIED_SOURCE = "No verified source"
_HYPOTHETICAL_EXAMPLE = "Hypothetical example"
_NOT_EVALUATED = "Not evaluated in this session"
_NO_UNSATISFACTORY = "No unsatisfactory items."
_NO_SATISFACTORY = "No satisfactory items."
_NO_SOURCES = "No sources were cited in this report."


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
        self.label = ParagraphStyle(
            "Label", parent=sample["BodyText"], fontSize=9, leading=11, textColor=colors.darkred
        )


def render_report_pdf(content: ReportContent) -> bytes:
    """Render the frozen report content as a PDF document and return its bytes."""
    styles = _Styles()
    story: list[Flowable] = [Paragraph(_TITLE, styles.title)]

    story.append(Paragraph("Summary", styles.heading))
    story.append(
        Paragraph(
            f"{_safe(content.adherence_percentage)}% adherence to the required skills",
            styles.percentage,
        )
    )
    story.append(Paragraph(f"Completed on {_long_date(content.completed_at)}", styles.body))
    story.append(_para(content.summary, styles.body))
    story.append(_para(content.disclaimer, styles.note))

    story.append(Paragraph("Performance by skill", styles.heading))
    story.append(_skills_table(content, styles))

    story.append(Paragraph("Questions and answers", styles.heading))
    for item in content.items:
        story.extend(_item_flowables(item, styles))

    story.append(Paragraph("Satisfactory points", styles.heading))
    satisfactory = [item for item in content.items if item.satisfactory]
    if satisfactory:
        story.append(
            _bullets(
                [
                    Paragraph(
                        f"{_heading_text(item)}: {_safe(item.question)}",
                        styles.body,
                    )
                    for item in satisfactory
                ]
            )
        )
    else:
        story.append(Paragraph(_NO_SATISFACTORY, styles.body))

    # Same order as the page: the order of `unsatisfactory_items`.
    by_position = {item.position: item for item in content.items}
    unsatisfactory = [
        by_position[position]
        for position in content.unsatisfactory_items
        if position in by_position
    ]
    story.append(Paragraph("Unsatisfactory items", styles.heading))
    if unsatisfactory:
        for item in unsatisfactory:
            story.extend(_improvement_flowables(item, styles))
    else:
        story.append(Paragraph(_NO_UNSATISFACTORY, styles.body))

    story.append(Paragraph("Sources", styles.heading))
    if content.sources_used:
        for source in content.sources_used:
            story.extend(_source_flowables(source, styles))
    else:
        story.append(Paragraph(_NO_SOURCES, styles.body))

    non_evaluated = [
        f"{_safe(name)} (desirable)" for name in content.non_evaluated.nice_to_have
    ] + [f"{_safe(name)} (non-technical)" for name in content.non_evaluated.non_technical]
    if non_evaluated:
        story.append(Paragraph(_NOT_EVALUATED, styles.heading))
        story.append(_bullets([Paragraph(entry, styles.body) for entry in non_evaluated]))

    story.append(Spacer(1, 0.5 * cm))
    story.append(
        _para(
            f"Model version: {content.model_version}. Rubric version: {content.rubric_version}.",
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


def _skills_table(content: ReportContent, styles: _Styles) -> Table:
    rows: list[list[Paragraph]] = [
        [
            Paragraph("<b>Skill</b>", styles.body),
            Paragraph("<b>Average score</b>", styles.body),
            Paragraph("<b>Questions</b>", styles.body),
        ]
    ]
    for skill in content.skills:
        rows.append(
            [
                _para(skill.skill, styles.body),
                _para(skill.average, styles.body),
                Paragraph(
                    ", ".join(str(position) for position in skill.question_positions),
                    styles.body,
                ),
            ]
        )
    table = Table(rows, colWidths=[8 * cm, 4 * cm, 5 * cm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def _heading_text(item: ReportItem) -> str:
    return f"Question {item.position} \u00b7 {_safe(item.skill)}"


def _item_flowables(item: ReportItem, styles: _Styles) -> list[Flowable]:
    flowables: list[Flowable] = [Paragraph(_heading_text(item), styles.subheading)]
    if item.no_verified_source:
        flowables.append(Paragraph(_NO_VERIFIED_SOURCE, styles.label))
    flowables.extend(
        [
            _para(item.question, styles.body),
            _labelled("Your answer", item.answer, styles.body),
            _labelled("Score", str(item.score), styles.body),
            _labelled("Justification", item.justification, styles.body),
        ]
    )
    if item.evidence_quotes:
        flowables.append(Paragraph("<b>Evidence from your answer:</b>", styles.body))
        flowables.append(
            _bullets(
                [
                    Paragraph(f"\u201c{_safe(quote)}\u201d", styles.body)
                    for quote in item.evidence_quotes
                ]
            )
        )
    return flowables


def _improvement_flowables(item: ReportItem, styles: _Styles) -> list[Flowable]:
    flowables: list[Flowable] = [Paragraph(_heading_text(item), styles.subheading)]
    if item.no_verified_source:
        flowables.append(Paragraph(_NO_VERIFIED_SOURCE, styles.label))
    flowables.append(_para(item.question, styles.body))
    if item.gap_explanation:
        flowables.append(_labelled("What was missing", item.gap_explanation, styles.body))
    reference = item.reference_answer
    if reference is None:
        return flowables
    flowables.append(_labelled("Reference answer", reference.text, styles.body))
    if reference.points:
        flowables.append(Paragraph("<b>Key points:</b>", styles.body))
        flowables.append(_bullets([_para(point, styles.body) for point in reference.points]))
    if reference.hypothetical_example:
        flowables.append(Paragraph("<b>Example:</b>", styles.body))
        flowables.append(Paragraph(_HYPOTHETICAL_EXAMPLE, styles.label))
        if reference.example_text:
            flowables.append(_para(reference.example_text, styles.body))
    elif reference.example_text:
        flowables.append(_labelled("Example", reference.example_text, styles.body))
    if not item.no_verified_source and reference.sources:
        flowables.append(Paragraph("<b>Sources:</b>", styles.body))
        for source in reference.sources:
            flowables.extend(_source_flowables(source, styles))
    return flowables


def _source_flowables(source: SourceRef, styles: _Styles) -> list[Flowable]:
    return [
        _para(source.title, styles.body),
        Paragraph(
            f"{_safe(source.url)} \u00b7 Collected on {_long_date(source.collected_at)}",
            styles.note,
        ),
        _para(source.excerpt, styles.body),
        Spacer(1, 0.2 * cm),
    ]


def _long_date(value: datetime) -> str:
    """Format a date like Angular's 'longDate' in en-US ("October 1, 2026"), locale-free."""
    return f"{_MONTHS[value.month - 1]} {value.day}, {value.year}"


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
