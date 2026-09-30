"""Unit tests for the optional OCR fallback (CT-29; OCR-01, OCR-02, CV-06).

The tesseract and poppler binaries are never called: ``convert_from_bytes`` and
``pytesseract.image_to_string`` are replaced by fakes.
"""

import io
import logging
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes.fake_llm import FakeLLM
from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPopplerTimeoutError
from pytesseract import TesseractNotFoundError  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.account import User
from app.models.resume import Resume, ResumeStatus
from app.resumes import ocr, processing, storage
from app.resumes.extraction import EXTRACTION_TASK
from app.resumes.ocr import (
    OCR_CONVERT_TIMEOUT_SECONDS,
    OCR_LANGUAGE,
    OCR_MAX_PAGES,
    OCR_PAGE_TIMEOUT_SECONDS,
    OcrNoText,
    ocr_pdf_text,
)
from app.resumes.processing import process_resume

REQUIRED_ENV = {
    "IR_THROTTLE_SECRET": "test-throttle-secret",
    "IR_OIDC_STATE_SECRET": "test-oidc-state-secret",
}

OCR_TEXT = "\n".join(
    [
        "Riley Placeholder - Senior Backend Engineer",
        "Experience: Backend Engineer at Northwind Logistics from 2019 to 2023.",
        "Built order routing services in Java and Spring Boot backed by PostgreSQL.",
        "Designed REST APIs consumed by thousands of warehouses every single day.",
        "Education: BSc Computer Science at Lakeside State University, 2015 - 2019.",
        "Skills: Java, Spring Boot, PostgreSQL, Docker, Kubernetes, testing and CI/CD.",
    ]
)

VALID_EXTRACTION = {
    "items": [
        {
            "kind": "experience",
            "fields": {
                "title": "Backend Engineer",
                "organization": "Northwind Logistics",
                "start": "2019",
                "end": "2023",
            },
            "origin": "explicit",
            "evidence": ["Backend Engineer at Northwind Logistics from 2019 to 2023."],
        },
    ]
}


@pytest.fixture(autouse=True)
def _settings_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("IR_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("IR_LLM_MAX_ATTEMPTS", "3")
    monkeypatch.delenv("IR_OCR_ENABLED", raising=False)
    monkeypatch.delenv("IR_MIN_RESUME_TEXT_CHARS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeEngine:
    """Records calls to the rasterizer and to tesseract and returns canned text per page."""

    def __init__(self, pages: list[str]) -> None:
        self.pages = pages
        self.languages: list[str] = []
        self.converted: list[bytes] = []
        self.convert_kwargs: list[dict[str, object]] = []
        self.ocr_kwargs: list[dict[str, object]] = []

    def convert(self, data: bytes, **kwargs: object) -> list[object]:
        self.converted.append(data)
        self.convert_kwargs.append(kwargs)
        return [object() for _ in self.pages]

    def image_to_string(self, image: object, lang: str = "", **kwargs: object) -> str:
        index = len(self.languages)
        self.languages.append(lang)
        self.ocr_kwargs.append(kwargs)
        return self.pages[index]


def _use_engine(monkeypatch: pytest.MonkeyPatch, pages: list[str]) -> _FakeEngine:
    engine = _FakeEngine(pages)
    monkeypatch.setattr(ocr, "convert_from_bytes", engine.convert)
    monkeypatch.setattr(ocr.pytesseract, "image_to_string", engine.image_to_string)
    return engine


def _scanned_pdf() -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    # Only shapes: simulates a scanned page without a text layer.
    pdf.rect(50, 50, 400, 600, fill=1)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


# --- ocr_pdf_text (CT-29) ------------------------------------------------------------------


def test_ocr_01_image_pdf_with_text_returns_the_ocr_text(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = OCR_TEXT.split("\n")
    engine = _use_engine(monkeypatch, ["\n".join(lines[:3]), "\n".join(lines[3:])])
    data = _scanned_pdf()

    text = ocr_pdf_text(data)

    assert text == OCR_TEXT
    assert engine.converted == [data]
    assert engine.languages == [OCR_LANGUAGE, OCR_LANGUAGE]
    assert OCR_LANGUAGE == "eng"


def test_ocr_02_empty_ocr_output_raises_ocr_no_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_engine(monkeypatch, [""])

    with pytest.raises(OcrNoText):
        ocr_pdf_text(_scanned_pdf())


def test_ocr_02_text_below_min_resume_text_chars_raises_ocr_no_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IR_MIN_RESUME_TEXT_CHARS", "50")
    get_settings.cache_clear()
    _use_engine(monkeypatch, ["a b c " * 8 + "\n\n   "])  # 24 non-whitespace chars

    with pytest.raises(OcrNoText):
        ocr_pdf_text(_scanned_pdf())


def test_ocr_02_min_resume_text_chars_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IR_MIN_RESUME_TEXT_CHARS", "10")
    get_settings.cache_clear()
    _use_engine(monkeypatch, ["Short but readable"])

    assert ocr_pdf_text(_scanned_pdf()) == "Short but readable"


def test_ocr_02_missing_poppler_raises_ocr_no_text_without_logging_content(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _missing(data: bytes, **_: object) -> list[object]:
        raise PDFInfoNotInstalledError("Unable to get page count. Is poppler installed?")

    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(ocr, "convert_from_bytes", _missing)
    monkeypatch.setattr(ocr, "log_event", lambda event, **fields: events.append((event, fields)))
    caplog.set_level(logging.DEBUG)

    with pytest.raises(OcrNoText):
        ocr_pdf_text(_scanned_pdf())

    assert events == [("resume.ocr_failed", {"error_code": "PDFInfoNotInstalledError"})]
    assert "poppler" not in caplog.text


def test_ocr_02_missing_tesseract_raises_ocr_no_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_engine(monkeypatch, ["unused"])

    def _missing(image: object, **_: object) -> str:
        raise TesseractNotFoundError()

    monkeypatch.setattr(ocr.pytesseract, "image_to_string", _missing)

    with pytest.raises(OcrNoText):
        ocr_pdf_text(_scanned_pdf())


def test_ocr_limits_rendered_pages_and_sets_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _use_engine(monkeypatch, [OCR_TEXT])

    ocr_pdf_text(_scanned_pdf())

    assert OCR_MAX_PAGES == 10
    assert engine.convert_kwargs == [
        {
            "dpi": 300,
            "first_page": 1,
            "last_page": OCR_MAX_PAGES,
            "timeout": OCR_CONVERT_TIMEOUT_SECONDS,
        }
    ]
    assert engine.ocr_kwargs == [{"timeout": OCR_PAGE_TIMEOUT_SECONDS}]
    assert 0 < OCR_PAGE_TIMEOUT_SECONDS <= OCR_CONVERT_TIMEOUT_SECONDS


def test_ocr_02_rasterization_timeout_raises_ocr_no_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def _slow(data: bytes, **_: object) -> list[object]:
        raise PDFPopplerTimeoutError("Run poppler timeout.")

    monkeypatch.setattr(ocr, "convert_from_bytes", _slow)

    with pytest.raises(OcrNoText):
        ocr_pdf_text(_scanned_pdf())


def test_ocr_02_tesseract_timeout_raises_ocr_no_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_engine(monkeypatch, ["unused"])

    def _slow(image: object, **_: object) -> str:
        raise RuntimeError("Tesseract process timeout")

    monkeypatch.setattr(ocr.pytesseract, "image_to_string", _slow)

    with pytest.raises(OcrNoText):
        ocr_pdf_text(_scanned_pdf())


def test_ocr_logs_never_contain_resume_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _use_engine(monkeypatch, [OCR_TEXT])
    caplog.set_level(logging.DEBUG)

    ocr_pdf_text(_scanned_pdf())

    assert "Northwind" not in caplog.text


# --- processing (CT-27 with OCR; OCR-01, CV-06) --------------------------------------------


def _resume(db: Session, data: bytes) -> Resume:
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    resume_id = uuid.uuid4()
    key = storage.save_file(user.id, resume_id, data)
    resume = Resume(
        id=resume_id,
        user_id=user.id,
        filename="cv.pdf",
        status=ResumeStatus.RECEIVED,
        storage_key=key,
    )
    db.add(resume)
    db.flush()
    return resume


def _run(db: Session, resume: Resume) -> Resume:
    process_resume(db, {"resume_id": str(resume.id)})
    db.expire_all()
    reloaded = db.execute(select(Resume).where(Resume.id == resume.id)).scalar_one()
    return reloaded


def test_ocr_01_processing_with_ocr_enabled_and_ocr_text_becomes_ready(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IR_OCR_ENABLED", "true")
    get_settings.cache_clear()
    engine = _use_engine(monkeypatch, [OCR_TEXT])
    llm = FakeLLM({EXTRACTION_TASK: [VALID_EXTRACTION]})
    monkeypatch.setattr(processing, "get_llm_client", lambda: llm)
    resume = _resume(db, _scanned_pdf())

    result = _run(db, resume)

    assert result.status is ResumeStatus.READY
    assert result.failure_code is None
    assert result.extracted_text == OCR_TEXT
    assert result.extraction is not None
    assert len(engine.converted) == 1


def test_ocr_02_processing_with_ocr_enabled_and_no_ocr_text_fails_with_ocr_no_text(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IR_OCR_ENABLED", "true")
    get_settings.cache_clear()
    _use_engine(monkeypatch, [""])
    llm = FakeLLM({})
    monkeypatch.setattr(processing, "get_llm_client", lambda: llm)
    resume = _resume(db, _scanned_pdf())

    result = _run(db, resume)

    assert result.status is ResumeStatus.FAILED
    assert result.failure_code == "OCR_NO_TEXT"
    assert result.extraction is None
    assert llm.calls == []


def test_cv_06_processing_with_ocr_disabled_fails_with_no_text_and_skips_ocr(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _use_engine(monkeypatch, [OCR_TEXT])
    llm = FakeLLM({})
    monkeypatch.setattr(processing, "get_llm_client", lambda: llm)
    resume = _resume(db, _scanned_pdf())

    result = _run(db, resume)

    assert result.status is ResumeStatus.FAILED
    assert result.failure_code == "NO_TEXT"
    assert result.extraction is None
    assert engine.converted == []
