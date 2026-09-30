"""Integration tests for the asynchronous resume processing job (CT-27)."""

import io
import json
import logging
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes.fake_llm import FakeLLM
from pydantic import BaseModel
from reportlab.lib.pagesizes import A4
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.pdfgen import canvas
from sqlalchemy import delete, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine
from app.jobs.registry import default_registry
from app.llm.client import LLMUnavailable
from app.models.account import User
from app.models.resume import ExtractionItem, Resume, ResumeStatus
from app.resumes import processing, storage
from app.resumes.extraction import EXTRACTION_TASK
from app.resumes.processing import RESUME_PROCESS_JOB, process_resume

ENGLISH_LINES = [
    "Riley Placeholder - Senior Backend Engineer",
    "Experience: Backend Engineer at Northwind Logistics from 2019 to 2023.",
    "Built order routing services in Java and Spring Boot backed by PostgreSQL.",
    "Designed REST APIs consumed by thousands of warehouses every single day.",
    "Education: BSc Computer Science at Lakeside State University, 2015 - 2019.",
    "Skills: Java, Spring Boot, PostgreSQL, Docker, Kubernetes, testing and CI/CD.",
]

PORTUGUESE_LINES = [
    "Maria Placeholder - Engenheira de Software Senior",
    "Experiencia: Engenheira de backend na Empresa Exemplo entre 2019 e 2023.",
    "Desenvolvi servicos de roteamento de pedidos usando Java e banco de dados.",
    "Projetei interfaces consumidas por milhares de armazens todos os dias.",
    "Formacao: Bacharelado em Ciencia da Computacao na Universidade Estadual.",
    "Habilidades: Java, bancos de dados relacionais, conteineres e testes automatizados.",
]

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
        {
            "kind": "skill",
            "fields": {"name": "Java"},
            "origin": "explicit",
            "evidence": ["Skills: Java, Spring Boot"],
        },
    ]
}

UNSUPPORTED_EXTRACTION = {
    "items": [
        {
            "kind": "skill",
            "fields": {"name": "Rust"},
            "origin": "explicit",
            "evidence": ["Expert in Rust"],
        }
    ]
}


@pytest.fixture(autouse=True)
def settings_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "storage"
    monkeypatch.setenv("IR_STORAGE_DIR", str(root))
    monkeypatch.setenv("IR_LLM_MAX_ATTEMPTS", "3")
    monkeypatch.delenv("IR_OCR_ENABLED", raising=False)
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def _make_pdf(lines: list[str] | None, encrypt: StandardEncryption | None = None) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, encrypt=encrypt)
    if lines is None:
        # Only shapes: simulates a scanned page without a text layer.
        pdf.rect(50, 50, 400, 600, fill=1)
    else:
        y = 800
        for line in lines:
            pdf.drawString(50, y, line)
            y -= 20
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _use_llm(monkeypatch: pytest.MonkeyPatch, llm: object) -> None:
    monkeypatch.setattr(processing, "get_llm_client", lambda: llm)


def _user(db: Session) -> User:
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    return user


def _resume(
    db: Session, data: bytes | None, status: ResumeStatus = ResumeStatus.RECEIVED
) -> Resume:
    user = _user(db)
    resume_id = uuid.uuid4()
    key = storage.save_file(user.id, resume_id, data) if data is not None else None
    resume = Resume(
        id=resume_id, user_id=user.id, filename="cv.pdf", status=status, storage_key=key
    )
    db.add(resume)
    db.flush()
    return resume


def _reload(db: Session, resume_id: uuid.UUID) -> Resume | None:
    db.expire_all()
    return db.execute(select(Resume).where(Resume.id == resume_id)).scalar_one_or_none()


def _run(db: Session, resume: Resume) -> Resume:
    process_resume(db, {"resume_id": str(resume.id)})
    reloaded = _reload(db, resume.id)
    assert reloaded is not None
    return reloaded


# --- registration (CT-7, CT-27) ------------------------------------------------------------


def test_cv_05_handler_is_registered_for_resume_process() -> None:
    assert RESUME_PROCESS_JOB == "resume.process"
    assert default_registry.handler_for("resume.process") is process_resume


# --- success (CV-05, CV-07) ----------------------------------------------------------------


def test_cv_07_english_pdf_with_valid_llm_output_becomes_ready(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM({EXTRACTION_TASK: [VALID_EXTRACTION]})
    _use_llm(monkeypatch, llm)
    resume = _resume(db, _make_pdf(ENGLISH_LINES))

    result = _run(db, resume)

    assert result.status is ResumeStatus.READY
    assert result.failure_code is None
    assert result.extracted_text is not None
    assert "Northwind Logistics" in result.extracted_text
    assert result.extraction is not None
    items = [ExtractionItem.model_validate(item) for item in result.extraction]
    assert {item.kind for item in items} == {"experience", "skill"}
    assert all(item.evidence for item in items)
    assert len(llm.calls_for(EXTRACTION_TASK)) == 1


# --- PDF failures (CV-06, CV-92) -----------------------------------------------------------


def test_cv_06_pdf_without_text_fails_with_no_text_and_no_extraction(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM({})
    _use_llm(monkeypatch, llm)
    resume = _resume(db, _make_pdf(None))

    result = _run(db, resume)

    assert result.status is ResumeStatus.FAILED
    assert result.failure_code == "NO_TEXT"
    assert result.extraction is None
    assert result.extracted_text is None
    assert llm.calls == []


def test_cv_06_ocr_enabled_calls_fallback_and_fails_with_ocr_no_text(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IR_OCR_ENABLED", "true")
    get_settings.cache_clear()
    _use_llm(monkeypatch, FakeLLM({}))
    resume = _resume(db, _make_pdf(None))

    result = _run(db, resume)

    assert result.status is ResumeStatus.FAILED
    assert result.failure_code == "OCR_NO_TEXT"
    assert result.extraction is None


def test_cv_92_corrupted_pdf_fails_with_corrupted(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_llm(monkeypatch, FakeLLM({}))
    resume = _resume(db, b"%PDF-1.4\n" + b"\x00garbage" * 50)

    result = _run(db, resume)

    assert result.status is ResumeStatus.FAILED
    assert result.failure_code == "CORRUPTED"
    assert result.extraction is None


def test_cv_92_password_protected_pdf_fails_with_protected(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_llm(monkeypatch, FakeLLM({}))
    encrypted = _make_pdf(
        ENGLISH_LINES, encrypt=StandardEncryption("user-pass", "owner-pass", strength=128)
    )
    resume = _resume(db, encrypted)

    result = _run(db, resume)

    assert result.status is ResumeStatus.FAILED
    assert result.failure_code == "PROTECTED"
    assert result.extraction is None


def test_cv_92_missing_stored_file_fails_with_corrupted(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_llm(monkeypatch, FakeLLM({}))
    resume = _resume(db, _make_pdf(ENGLISH_LINES))
    assert resume.storage_key is not None
    storage.delete_file(resume.storage_key)

    result = _run(db, resume)

    assert result.status is ResumeStatus.FAILED
    assert result.failure_code == "CORRUPTED"


# --- language (CV-14) ----------------------------------------------------------------------


def test_cv_14_portuguese_resume_fails_with_not_english(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM({})
    _use_llm(monkeypatch, llm)
    resume = _resume(db, _make_pdf(PORTUGUESE_LINES))

    result = _run(db, resume)

    assert result.status is ResumeStatus.FAILED
    assert result.failure_code == "NOT_ENGLISH"
    assert result.extraction is None
    assert result.extracted_text is None
    assert llm.calls == []


# --- LLM failures (CV-09, CV-93, KNOW-92) --------------------------------------------------


def test_cv_93_llm_always_unavailable_fails_after_llm_max_attempts(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM({EXTRACTION_TASK: [LLMUnavailable] * 3})
    _use_llm(monkeypatch, llm)
    resume = _resume(db, _make_pdf(ENGLISH_LINES))

    result = _run(db, resume)

    assert result.status is ResumeStatus.FAILED
    assert result.failure_code == "LLM_UNAVAILABLE"
    assert result.extraction is None
    assert len(llm.calls_for(EXTRACTION_TASK)) == 3
    assert llm.remaining(EXTRACTION_TASK) == 0


def test_know_92_llm_unavailable_keeps_the_resume_and_its_file(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_llm(monkeypatch, FakeLLM({EXTRACTION_TASK: [LLMUnavailable] * 3}))
    resume = _resume(db, _make_pdf(ENGLISH_LINES))
    key = resume.storage_key
    assert key is not None

    result = _run(db, resume)

    assert result.status is ResumeStatus.FAILED
    assert storage.read_file(key)


def test_cv_09_invalid_output_after_attempts_fails_with_extraction_invalid(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM({EXTRACTION_TASK: [{"wrong": "shape"}] * 3})
    _use_llm(monkeypatch, llm)
    resume = _resume(db, _make_pdf(ENGLISH_LINES))

    result = _run(db, resume)

    assert result.status is ResumeStatus.FAILED
    assert result.failure_code == "EXTRACTION_INVALID"
    assert result.extraction is None
    assert len(llm.calls_for(EXTRACTION_TASK)) == 3


def test_cv_09_extraction_without_supported_items_fails_with_extraction_invalid(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_llm(monkeypatch, FakeLLM({EXTRACTION_TASK: [UNSUPPORTED_EXTRACTION]}))
    resume = _resume(db, _make_pdf(ENGLISH_LINES))

    result = _run(db, resume)

    assert result.status is ResumeStatus.FAILED
    assert result.failure_code == "EXTRACTION_INVALID"
    assert result.extraction is None


# --- deletion during processing (CV-96) ----------------------------------------------------


class _DeletingLLM:
    """Deletes the resume while the extraction is running, then answers normally."""

    def __init__(self, db: Session, resume_id: uuid.UUID) -> None:
        self._db = db
        self._resume_id = resume_id
        self._inner = FakeLLM({EXTRACTION_TASK: [VALID_EXTRACTION]})

    def complete_structured[T: BaseModel](
        self, task: str, system: str, user: str, output_model: type[T]
    ) -> T:
        self._db.execute(delete(Resume).where(Resume.id == self._resume_id))
        return self._inner.complete_structured(task, system, user, output_model)


def test_cv_96_resume_deleted_during_extraction_is_not_recreated(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume = _resume(db, _make_pdf(ENGLISH_LINES))
    resume_id = resume.id
    _use_llm(monkeypatch, _DeletingLLM(db, resume_id))

    process_resume(db, {"resume_id": str(resume_id)})
    db.flush()

    count = db.execute(
        select(func.count()).select_from(Resume).where(Resume.id == resume_id)
    ).scalar_one()
    assert count == 0


def test_cv_96_unknown_resume_is_ignored(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeLLM({})
    _use_llm(monkeypatch, llm)

    process_resume(db, {"resume_id": str(uuid.uuid4())})

    assert llm.calls == []


# --- no reprocessing (spec 7.1) ------------------------------------------------------------


@pytest.mark.parametrize("status", [ResumeStatus.FAILED, ResumeStatus.READY])
def test_cv_05_finished_resume_is_not_reprocessed(
    db: Session, monkeypatch: pytest.MonkeyPatch, status: ResumeStatus
) -> None:
    llm = FakeLLM({EXTRACTION_TASK: [VALID_EXTRACTION]})
    _use_llm(monkeypatch, llm)
    resume = _resume(db, _make_pdf(ENGLISH_LINES), status=status)
    resume.failure_code = "NO_TEXT" if status is ResumeStatus.FAILED else None
    db.flush()

    result = _run(db, resume)

    assert result.status is status
    assert result.extraction is None
    assert llm.calls == []


# --- payload validation --------------------------------------------------------------------


@pytest.mark.parametrize("payload", [{}, {"resume_id": "not-a-uuid"}])
def test_cv_05_invalid_payload_is_rejected(db: Session, payload: dict[str, str]) -> None:
    with pytest.raises(ValueError) as exc_info:
        process_resume(db, payload)
    assert "not-a-uuid" not in str(exc_info.value)


# --- visibility of the processing state (CV-05) --------------------------------------------


@pytest.fixture
def committed_resume(migrated_database: str) -> Iterator[uuid.UUID]:
    """A resume committed for real, so other connections can see its state."""
    with Session(get_engine()) as session:
        user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
        session.add(user)
        session.flush()
        resume_id = uuid.uuid4()
        key = storage.save_file(user.id, resume_id, _make_pdf(ENGLISH_LINES))
        session.add(
            Resume(
                id=resume_id,
                user_id=user.id,
                filename="cv.pdf",
                status=ResumeStatus.RECEIVED,
                storage_key=key,
            )
        )
        session.commit()
        user_id = user.id
    yield resume_id
    with Session(get_engine()) as session:
        session.execute(delete(User).where(User.id == user_id))
        session.commit()


class _ObservingLLM:
    """Records the resume status seen by another connection while the LLM is running."""

    def __init__(self, resume_id: uuid.UUID) -> None:
        self._resume_id = resume_id
        self.seen: list[ResumeStatus] = []
        self._inner = FakeLLM({EXTRACTION_TASK: [VALID_EXTRACTION]})

    def complete_structured[T: BaseModel](
        self, task: str, system: str, user: str, output_model: type[T]
    ) -> T:
        with Session(get_engine()) as other:
            self.seen.append(
                other.execute(
                    select(Resume.status).where(Resume.id == self._resume_id)
                ).scalar_one()
            )
        return self._inner.complete_structured(task, system, user, output_model)


def test_cv_05_processing_state_is_visible_while_extracting(
    committed_resume: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _ObservingLLM(committed_resume)
    _use_llm(monkeypatch, llm)

    with Session(get_engine()) as session:
        process_resume(session, {"resume_id": str(committed_resume)})
        session.commit()

    assert llm.seen == [ResumeStatus.PROCESSING]
    with Session(get_engine()) as session:
        status = session.execute(
            select(Resume.status).where(Resume.id == committed_resume)
        ).scalar_one()
    assert status is ResumeStatus.READY


# --- logging (AS-3) ------------------------------------------------------------------------


def test_cv_07_logs_never_contain_resume_text(
    db: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _use_llm(monkeypatch, FakeLLM({EXTRACTION_TASK: [VALID_EXTRACTION]}))
    resume = _resume(db, _make_pdf(ENGLISH_LINES))

    result = _run(db, resume)

    assert result.status is ResumeStatus.READY
    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    changed = [e for e in events if e.get("event") == "resume.status_changed"]
    assert changed
    assert changed[-1]["resume_id"] == str(resume.id)
    assert changed[-1]["to"] == "ready"
    assert isinstance(changed[-1]["duration_ms"], int)
    raw = caplog.text + json.dumps([vars(record) for record in caplog.records], default=str)
    for fragment in ("Northwind", "Riley", "Lakeside", "order routing", "cv.pdf"):
        assert fragment not in raw


# --- unexpected errors never leave a version in processing (QA finding TASK-031-1) --------


def _committed_state(resume_id: uuid.UUID) -> tuple[ResumeStatus, str | None, object]:
    with Session(get_engine()) as session:
        row = session.execute(
            select(Resume.status, Resume.failure_code, Resume.extraction).where(
                Resume.id == resume_id
            )
        ).one()
    return row[0], row[1], row[2]


def test_cv_05_major_oserror_on_read_marks_failed(
    committed_resume: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_llm(monkeypatch, FakeLLM({EXTRACTION_TASK: [VALID_EXTRACTION]}))

    def denied(key: str) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(storage, "read_file", denied)

    with Session(get_engine()) as session, pytest.raises(PermissionError):
        process_resume(session, {"resume_id": str(committed_resume)})

    status, failure_code, extraction = _committed_state(committed_resume)
    assert status is ResumeStatus.FAILED
    assert failure_code == "LLM_UNAVAILABLE"
    assert extraction is None


def test_cv_93_major_misconfigured_llm_host_marks_llm_unavailable(
    committed_resume: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IR_LLM_BASE_URL", "http://ollama.internal:11434/v1")
    get_settings.cache_clear()

    with Session(get_engine()) as session:
        process_resume(session, {"resume_id": str(committed_resume)})
        session.commit()

    status, failure_code, extraction = _committed_state(committed_resume)
    assert status is ResumeStatus.FAILED
    assert failure_code == "LLM_UNAVAILABLE"
    assert extraction is None


def test_know_92_major_db_error_at_lock_marks_failed(
    committed_resume: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_llm(monkeypatch, FakeLLM({EXTRACTION_TASK: [VALID_EXTRACTION]}))

    def broken_lock(db: Session, resume_id: uuid.UUID) -> Resume | None:
        raise OperationalError("SELECT", {}, Exception("server closed the connection"))

    monkeypatch.setattr(processing, "_lock_resume", broken_lock)

    with Session(get_engine()) as session, pytest.raises(OperationalError):
        process_resume(session, {"resume_id": str(committed_resume)})

    status, failure_code, extraction = _committed_state(committed_resume)
    assert status is ResumeStatus.FAILED
    assert failure_code == "LLM_UNAVAILABLE"
    assert extraction is None


def test_cv_05_major_error_after_lock_does_not_block_failure_mark(
    committed_resume: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row lock of the worker session is released before the fresh session writes."""
    _use_llm(monkeypatch, FakeLLM({EXTRACTION_TASK: [VALID_EXTRACTION]}))
    original_lock = processing._lock_resume

    def lock_then_fail(db: Session, resume_id: uuid.UUID) -> Resume | None:
        original_lock(db, resume_id)
        raise OperationalError("UPDATE", {}, Exception("connection lost"))

    monkeypatch.setattr(processing, "_lock_resume", lock_then_fail)
    with Session(get_engine()) as session, pytest.raises(OperationalError):
        process_resume(session, {"resume_id": str(committed_resume)})

    status, failure_code, _ = _committed_state(committed_resume)
    assert status is ResumeStatus.FAILED
    assert failure_code == "LLM_UNAVAILABLE"
