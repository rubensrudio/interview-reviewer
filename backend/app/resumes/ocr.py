"""Optional OCR fallback for scanned resumes (CT-29; OCR-01, OCR-02, DA-16).

Pages are rasterized with ``pdf2image`` (poppler) and read with ``pytesseract`` (tesseract),
both installed on the host, so the resume never leaves the private infrastructure. The
fallback only runs when ``ocr_enabled`` is set; processing calls it after ``PdfNoText``.

When the OCR engine fails (binary missing, page rasterization or tesseract error) or reads
less than ``min_resume_text_chars`` non-whitespace characters, ``OcrNoText`` is raised: the
text could not be read. Logs carry only the error class, never resume content.
"""

import pytesseract  # type: ignore[import-untyped]
from pdf2image import convert_from_bytes
from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPageCountError, PDFSyntaxError

from app.config import get_settings
from app.observability import log_event
from app.resumes.pdf import PdfNoText

__all__ = ["OCR_DPI", "OCR_LANGUAGE", "OcrNoText", "ocr_pdf_text"]

OCR_LANGUAGE = "eng"
OCR_DPI = 300

_ENGINE_ERRORS: tuple[type[Exception], ...] = (
    PDFInfoNotInstalledError,
    PDFPageCountError,
    PDFSyntaxError,
    pytesseract.TesseractNotFoundError,
    pytesseract.TesseractError,
    RuntimeError,  # pytesseract timeout
    OSError,
    ValueError,
)


class OcrNoText(PdfNoText):
    """OCR produced no usable text for the PDF."""


def ocr_pdf_text(data: bytes) -> str:
    """Read the text of a PDF by OCR. Raises ``OcrNoText`` when no usable text is produced."""
    try:
        images = convert_from_bytes(data, dpi=OCR_DPI)
        pages = [str(pytesseract.image_to_string(image, lang=OCR_LANGUAGE)) for image in images]
    except _ENGINE_ERRORS as exc:
        log_event("resume.ocr_failed", error_code=type(exc).__name__)
        raise OcrNoText("OCR could not read the PDF") from exc

    text = "\n".join(pages).strip()
    if _non_whitespace_length(text) < get_settings().min_resume_text_chars:
        raise OcrNoText("OCR produced no usable text")
    return text


def _non_whitespace_length(text: str) -> int:
    return sum(1 for char in text if not char.isspace())
