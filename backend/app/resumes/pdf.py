"""PDF sniffing and text extraction for uploaded resumes (CV-02, CV-06, CV-90, CV-92)."""

import io
import logging

from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError, PdfStreamError

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"
MAGIC_SEARCH_WINDOW = 1024

# Mirrors the ``min_resume_text_chars`` default from plan section 7.7.
MIN_RESUME_TEXT_CHARS = 200


class PdfError(Exception):
    """Base class for PDF processing failures."""


class PdfNoText(PdfError):
    """The PDF has no usable text layer (e.g. a scanned document)."""


class PdfCorrupted(PdfError):
    """The PDF could not be parsed."""


class PdfEncrypted(PdfError):
    """The PDF is protected by a password and cannot be read."""


def looks_like_pdf(data: bytes) -> bool:
    """Return True when the content carries the PDF signature near the start."""
    if not data:
        return False
    return PDF_MAGIC in data[:MAGIC_SEARCH_WINDOW]


def extract_pdf_text(data: bytes) -> str:
    """Extract the text layer of a PDF.

    Raises ``PdfEncrypted`` for password-protected files, ``PdfCorrupted`` for
    unparseable files and ``PdfNoText`` when the text layer is too small.
    """
    if not looks_like_pdf(data):
        raise PdfCorrupted("content is not a PDF")
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and not _try_empty_password(reader):
            raise PdfEncrypted("PDF requires a password")
        pages = [page.extract_text() or "" for page in reader.pages]
    except PdfEncrypted:
        raise
    except FileNotDecryptedError as exc:
        raise PdfEncrypted("PDF requires a password") from exc
    except Exception as exc:  # pypdf raises many exception types on malformed input
        logger.info("pdf parsing failed: %s", type(exc).__name__)
        raise PdfCorrupted("PDF could not be parsed") from exc

    text = "\n".join(pages).strip()
    if _non_whitespace_length(text) < MIN_RESUME_TEXT_CHARS:
        raise PdfNoText("PDF has no readable text layer")
    return text


def _try_empty_password(reader: PdfReader) -> bool:
    """Open files protected only by an owner password (empty user password)."""
    try:
        return bool(reader.decrypt(""))
    except (PdfReadError, PdfStreamError, NotImplementedError):
        return False


def _non_whitespace_length(text: str) -> int:
    return sum(1 for char in text if not char.isspace())
