import io

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.pdfgen import canvas

from app.resumes.pdf import (
    PdfCorrupted,
    PdfEncrypted,
    PdfNoText,
    extract_pdf_text,
    looks_like_pdf,
)

_RESUME_LINES = [
    "Jane Doe - Senior Backend Engineer",
    "Experience: 8 years building distributed systems with Python and Java.",
    "Led the migration of a monolith to microservices on Kubernetes.",
    "Designed REST APIs consumed by millions of users every day.",
    "Skills: Python, FastAPI, PostgreSQL, Docker, AWS, CI/CD, testing.",
    "Education: BSc in Computer Science, State University.",
]

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _make_pdf(lines: list[str] | None, encrypt: StandardEncryption | None = None) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, encrypt=encrypt)
    if lines is None:
        # Drawing only shapes simulates a scanned page without a text layer.
        pdf.rect(50, 50, 400, 600, fill=1)
    else:
        y = 800
        for line in lines:
            pdf.drawString(50, y, line)
            y -= 20
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def test_cv_90_empty_bytes_do_not_look_like_pdf() -> None:
    assert looks_like_pdf(b"") is False


def test_cv_02_png_does_not_look_like_pdf() -> None:
    assert looks_like_pdf(_PNG_BYTES) is False


def test_cv_02_generated_pdf_looks_like_pdf() -> None:
    assert looks_like_pdf(_make_pdf(_RESUME_LINES)) is True


def test_cv_02_pdf_marker_after_first_1024_bytes_is_rejected() -> None:
    assert looks_like_pdf(b" " * 1024 + b"%PDF-1.7") is False


def test_cv_02_pdf_marker_within_first_1024_bytes_is_accepted() -> None:
    assert looks_like_pdf(b" " * 10 + b"%PDF-1.7\n") is True


def test_cv_06_extracts_text_from_generated_pdf() -> None:
    text = extract_pdf_text(_make_pdf(_RESUME_LINES))
    assert "Senior Backend Engineer" in text
    assert "Kubernetes" in text


def test_cv_06_pdf_without_text_layer_raises_no_text() -> None:
    with pytest.raises(PdfNoText):
        extract_pdf_text(_make_pdf(None))


def test_cv_06_pdf_with_too_little_text_raises_no_text() -> None:
    with pytest.raises(PdfNoText):
        extract_pdf_text(_make_pdf(["Jane Doe"]))


def test_cv_92_truncated_pdf_raises_corrupted() -> None:
    data = _make_pdf(_RESUME_LINES)
    with pytest.raises(PdfCorrupted):
        extract_pdf_text(data[: len(data) // 3])


def test_cv_92_garbage_after_header_raises_corrupted() -> None:
    with pytest.raises(PdfCorrupted):
        extract_pdf_text(b"%PDF-1.4\n" + b"\x00garbage" * 50)


def test_cv_92_password_protected_pdf_raises_encrypted() -> None:
    encryption = StandardEncryption("user-secret", "owner-secret")
    with pytest.raises(PdfEncrypted):
        extract_pdf_text(_make_pdf(_RESUME_LINES, encrypt=encryption))


def test_cv_92_owner_only_protected_pdf_is_readable() -> None:
    encryption = StandardEncryption("", "owner-secret")
    text = extract_pdf_text(_make_pdf(_RESUME_LINES, encrypt=encryption))
    assert "Kubernetes" in text
