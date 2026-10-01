"""Optional OCR fallback for scanned resumes (CT-29; OCR-01, OCR-02, DA-16).

Pages are rasterized with ``pdf2image`` (poppler) and read with ``pytesseract`` (tesseract),
both installed on the host, so the resume never leaves the private infrastructure. The
fallback only runs when ``ocr_enabled`` is set; processing calls it after ``PdfNoText``.

When the OCR engine fails (binary missing, page rasterization or tesseract error) or reads
less than ``min_resume_text_chars`` non-whitespace characters, ``OcrNoText`` is raised: the
text could not be read. Logs carry only the error class, never resume content.

To keep a hostile PDF from exhausting the worker, only the first ``OCR_MAX_PAGES`` pages are
rendered, rasterization is bounded by ``OCR_CONVERT_TIMEOUT_SECONDS`` and each page read by
``OCR_PAGE_TIMEOUT_SECONDS``; a timeout ends as ``OcrNoText``. Every page is scaled so its
longest side is ``OCR_MAX_PAGE_SIDE_PX`` pixels in grayscale, so a page with a huge declared
size cannot blow up memory; Pillow's decompression bomb guard still maps to ``OcrNoText``.
"""

import pytesseract  # type: ignore[import-untyped]
from pdf2image import convert_from_bytes
from pdf2image.exceptions import (
    PDFInfoNotInstalledError,
    PDFPageCountError,
    PDFPopplerTimeoutError,
    PDFSyntaxError,
)
from PIL.Image import DecompressionBombError

from app.config import get_settings
from app.observability import log_event
from app.resumes.pdf import PdfNoText

__all__ = [
    "OCR_CONVERT_TIMEOUT_SECONDS",
    "OCR_DPI",
    "OCR_LANGUAGE",
    "OCR_MAX_PAGE_SIDE_PX",
    "OCR_MAX_PAGES",
    "OCR_PAGE_TIMEOUT_SECONDS",
    "OcrNoText",
    "ocr_pdf_text",
]

OCR_LANGUAGE = "eng"
OCR_DPI = 300
# A resume fits in a few pages; later pages are ignored.
OCR_MAX_PAGES = 10
# Longest side of a rendered page: A4 at 300 DPI. Caps pixels whatever the page size.
OCR_MAX_PAGE_SIDE_PX = 3508
OCR_CONVERT_TIMEOUT_SECONDS = 60
OCR_PAGE_TIMEOUT_SECONDS = 30

_ENGINE_ERRORS: tuple[type[Exception], ...] = (
    PDFInfoNotInstalledError,
    PDFPageCountError,
    PDFPopplerTimeoutError,
    PDFSyntaxError,
    DecompressionBombError,
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
        images = convert_from_bytes(
            data,
            dpi=OCR_DPI,
            first_page=1,
            last_page=OCR_MAX_PAGES,
            timeout=OCR_CONVERT_TIMEOUT_SECONDS,
            size=OCR_MAX_PAGE_SIDE_PX,
            grayscale=True,
        )
        pages = [
            str(
                pytesseract.image_to_string(
                    image, lang=OCR_LANGUAGE, timeout=OCR_PAGE_TIMEOUT_SECONDS
                )
            )
            for image in images
        ]
    except _ENGINE_ERRORS as exc:
        log_event("resume.ocr_failed", error_code=type(exc).__name__)
        raise OcrNoText("OCR could not read the PDF") from exc

    text = "\n".join(pages).strip()
    if _non_whitespace_length(text) < get_settings().min_resume_text_chars:
        raise OcrNoText("OCR produced no usable text")
    return text


def _non_whitespace_length(text: str) -> int:
    return sum(1 for char in text if not char.isspace())
