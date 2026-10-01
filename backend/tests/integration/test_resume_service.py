"""Integration tests for the resume upload and listing service (CT-25)."""

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_sessionmaker
from app.errors import (
    FILE_TOO_LARGE,
    INVALID_PDF,
    RESOURCE_NOT_FOUND,
    RESUME_LIMIT_REACHED,
    VALIDATION_ERROR,
    AppError,
)
from app.models.account import User
from app.models.job import Job, JobStatus
from app.models.resume import Resume, ResumeStatus
from app.resumes import storage
from app.resumes.service import get_owned_resume, list_resumes, upload_resume

PDF_HEADER = b"%PDF-1.4\n"
PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MAX_BYTES = 5_242_880


@pytest.fixture(autouse=True)
def storage_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "storage"
    monkeypatch.setenv("IR_STORAGE_DIR", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def _pdf(size: int = 1024) -> bytes:
    return PDF_HEADER + b"a" * (size - len(PDF_HEADER))


def _user(db: Session) -> User:
    user = User(email_normalized=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    db.flush()
    return user


def _add_resumes(db: Session, user: User, count: int, status: ResumeStatus) -> list[Resume]:
    resumes = [Resume(user_id=user.id, filename=f"cv-{i}.pdf", status=status) for i in range(count)]
    db.add_all(resumes)
    db.flush()
    return resumes


def _count_resumes(db: Session, user: User) -> int:
    return db.execute(
        select(func.count()).select_from(Resume).where(Resume.user_id == user.id)
    ).scalar_one()


def _count_jobs(db: Session) -> int:
    return db.execute(
        select(func.count()).select_from(Job).where(Job.kind == "resume.process")
    ).scalar_one()


def _stored_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()] if root.exists() else []


def _assert_nothing_written(db: Session, user: User, root: Path, jobs_before: int) -> None:
    assert _count_resumes(db, user) == 0
    assert _count_jobs(db) == jobs_before
    assert _stored_files(root) == []


# --- upload: size (CV-03, CV-91) --------------------------------------------------------


def test_cv_91_file_of_exactly_the_limit_is_accepted(db: Session, storage_dir: Path) -> None:
    user = _user(db)
    data = _pdf(MAX_BYTES)

    resume = upload_resume(db, user, "cv.pdf", data)

    assert resume.status is ResumeStatus.RECEIVED
    assert resume.storage_key is not None
    assert storage.read_file(resume.storage_key) == data


def test_cv_91_file_one_byte_above_the_limit_is_rejected(db: Session, storage_dir: Path) -> None:
    user = _user(db)
    jobs_before = _count_jobs(db)

    with pytest.raises(AppError) as exc_info:
        upload_resume(db, user, "cv.pdf", _pdf(MAX_BYTES + 1))

    assert exc_info.value.code == FILE_TOO_LARGE
    assert exc_info.value.status == 413
    assert exc_info.value.details == {"limit_bytes": MAX_BYTES}
    _assert_nothing_written(db, user, storage_dir, jobs_before)


def test_cv_03_size_limit_comes_from_settings(
    db: Session, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IR_MAX_PDF_BYTES", "2048")
    get_settings.cache_clear()
    user = _user(db)

    with pytest.raises(AppError) as exc_info:
        upload_resume(db, user, "cv.pdf", _pdf(2049))

    assert exc_info.value.code == FILE_TOO_LARGE
    assert exc_info.value.details == {"limit_bytes": 2048}


def test_cv_03_size_is_checked_before_content(db: Session, storage_dir: Path) -> None:
    user = _user(db)

    with pytest.raises(AppError) as exc_info:
        upload_resume(db, user, "cv.pdf", b"\x00" * (MAX_BYTES + 1))

    assert exc_info.value.code == FILE_TOO_LARGE


# --- upload: content (CV-02, CV-90) -----------------------------------------------------


def test_cv_90_empty_file_is_invalid_pdf(db: Session, storage_dir: Path) -> None:
    user = _user(db)
    jobs_before = _count_jobs(db)

    with pytest.raises(AppError) as exc_info:
        upload_resume(db, user, "cv.pdf", b"")

    assert exc_info.value.code == INVALID_PDF
    assert exc_info.value.status == 415
    _assert_nothing_written(db, user, storage_dir, jobs_before)


def test_cv_02_png_content_with_pdf_extension_is_invalid_pdf(
    db: Session, storage_dir: Path
) -> None:
    user = _user(db)
    jobs_before = _count_jobs(db)

    with pytest.raises(AppError) as exc_info:
        upload_resume(db, user, "photo.pdf", PNG_HEADER)

    assert exc_info.value.code == INVALID_PDF
    _assert_nothing_written(db, user, storage_dir, jobs_before)


@pytest.mark.parametrize("data", ["%PDF-1.4 text", None, 123, [1, 2]])
def test_cv_02_non_bytes_data_is_invalid_pdf(db: Session, data: object) -> None:
    user = _user(db)

    with pytest.raises(AppError) as exc_info:
        upload_resume(db, user, "cv.pdf", data)  # type: ignore[arg-type]

    assert exc_info.value.code == INVALID_PDF


# --- upload: limit of versions (CV-04, CV-95) -------------------------------------------


@pytest.mark.parametrize("status", list(ResumeStatus))
def test_cv_04_upload_with_ten_versions_is_rejected(
    db: Session, storage_dir: Path, status: ResumeStatus
) -> None:
    user = _user(db)
    _add_resumes(db, user, 10, status)
    jobs_before = _count_jobs(db)

    with pytest.raises(AppError) as exc_info:
        upload_resume(db, user, "cv.pdf", _pdf())

    assert exc_info.value.code == RESUME_LIMIT_REACHED
    assert exc_info.value.status == 409
    assert _count_resumes(db, user) == 10
    assert _count_jobs(db) == jobs_before
    assert _stored_files(storage_dir) == []


def test_cv_04_upload_with_nine_versions_is_accepted(db: Session) -> None:
    user = _user(db)
    _add_resumes(db, user, 9, ResumeStatus.READY)

    upload_resume(db, user, "cv.pdf", _pdf())

    assert _count_resumes(db, user) == 10


def test_cv_04_other_users_versions_do_not_count(db: Session) -> None:
    other = _user(db)
    _add_resumes(db, other, 10, ResumeStatus.READY)
    user = _user(db)

    upload_resume(db, user, "cv.pdf", _pdf())

    assert _count_resumes(db, user) == 1


@pytest.fixture
def committed_users(migrated_database: str) -> Iterator[list[uuid.UUID]]:
    user_ids: list[uuid.UUID] = []
    yield user_ids
    with get_sessionmaker()() as session:
        resume_ids = [
            str(resume_id)
            for resume_id in session.execute(
                select(Resume.id).where(Resume.user_id.in_(user_ids))
            ).scalars()
        ]
        session.execute(
            delete(Job).where(
                Job.kind == "resume.process", Job.payload["resume_id"].astext.in_(resume_ids)
            )
        )
        session.execute(delete(User).where(User.id.in_(user_ids)))
        session.commit()


def test_cv_95_concurrent_uploads_with_nine_versions_accept_only_one(
    committed_users: list[uuid.UUID], storage_dir: Path
) -> None:
    with get_sessionmaker()() as session:
        user = _user(session)
        _add_resumes(session, user, 9, ResumeStatus.READY)
        session.commit()
        user_id = user.id
    committed_users.append(user_id)

    workers = 2
    barrier = threading.Barrier(workers)
    lock = threading.Lock()
    accepted: list[uuid.UUID] = []
    rejected: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with get_sessionmaker()() as session:
                owner = session.get(User, user_id)
                assert owner is not None
                barrier.wait(timeout=10)
                try:
                    resume = upload_resume(session, owner, "cv.pdf", _pdf())
                    session.commit()
                except AppError as error:
                    session.rollback()
                    with lock:
                        rejected.append(error.code)
                    return
                with lock:
                    accepted.append(resume.id)
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
            with lock:
                errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(accepted) == 1
    assert rejected == [RESUME_LIMIT_REACHED]
    with get_sessionmaker()() as session:
        count = session.execute(
            select(func.count()).select_from(Resume).where(Resume.user_id == user_id)
        ).scalar_one()
        assert count == 10
    assert len(_stored_files(storage_dir)) == 1


# --- upload: accepted (CV-01) -----------------------------------------------------------


def test_cv_01_accepted_upload_stores_file_and_received_version(
    db: Session, storage_dir: Path
) -> None:
    user = _user(db)
    data = _pdf()

    resume = upload_resume(db, user, "My Resume.pdf", data)

    stored = db.execute(select(Resume).where(Resume.id == resume.id)).scalar_one()
    assert stored.user_id == user.id
    assert stored.filename == "My Resume.pdf"
    assert stored.status is ResumeStatus.RECEIVED
    assert stored.uploaded_at is not None
    assert stored.storage_key == f"{user.id}/{resume.id}.pdf"
    assert storage.read_file(stored.storage_key) == data
    assert _stored_files(storage_dir) == [storage_dir / str(user.id) / f"{resume.id}.pdf"]


def test_cv_01_accepted_upload_enqueues_resume_process_job(db: Session) -> None:
    user = _user(db)

    resume = upload_resume(db, user, "cv.pdf", _pdf())

    jobs = (
        db.execute(
            select(Job).where(
                Job.kind == "resume.process",
                Job.payload["resume_id"].astext == str(resume.id),
            )
        )
        .scalars()
        .all()
    )
    assert len(jobs) == 1
    assert jobs[0].payload == {"resume_id": str(resume.id)}
    assert jobs[0].status is JobStatus.QUEUED


def test_cv_01_upload_does_not_process_the_pdf(db: Session) -> None:
    user = _user(db)

    resume = upload_resume(db, user, "cv.pdf", _pdf())

    assert resume.extracted_text is None
    assert resume.extraction is None


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("C:\\Users\\me\\cv.pdf", "cv.pdf"),
        ("../../etc/cv.pdf", "cv.pdf"),
        ("cv\x00.pdf", "cv.pdf"),
        ("cv\ud800.pdf", "cv.pdf"),
        ("  spaced.pdf  ", "spaced.pdf"),
        ("", "resume.pdf"),
        ("\x00\x01", "resume.pdf"),
        ("a" * 300 + ".pdf", "a" * 255),
    ],
)
def test_cv_01_filename_is_sanitized(db: Session, filename: str, expected: str) -> None:
    user = _user(db)

    resume = upload_resume(db, user, filename, _pdf())

    db.expire(resume)
    assert resume.filename == expected


@pytest.mark.parametrize("filename", [None, 123, b"cv.pdf"])
def test_cv_01_non_string_filename_is_validation_error(
    db: Session, storage_dir: Path, filename: object
) -> None:
    user = _user(db)
    jobs_before = _count_jobs(db)

    with pytest.raises(AppError) as exc_info:
        upload_resume(db, user, filename, _pdf())  # type: ignore[arg-type]

    assert exc_info.value.code == VALIDATION_ERROR
    _assert_nothing_written(db, user, storage_dir, jobs_before)


# --- list_resumes (CV-11, CV-13) --------------------------------------------------------


def test_cv_13_list_returns_only_own_versions_newest_first(db: Session) -> None:
    user = _user(db)
    other = _user(db)
    _add_resumes(db, other, 2, ResumeStatus.READY)
    first = upload_resume(db, user, "first.pdf", _pdf())
    second = Resume(user_id=user.id, filename="second.pdf", status=ResumeStatus.FAILED)
    db.add(second)
    db.flush()
    db.execute(
        Resume.__table__.update()
        .where(Resume.id == first.id)
        .values(uploaded_at=func.now() - func.make_interval(0, 0, 0, 1))
    )
    db.expire_all()

    resumes = list_resumes(db, user)

    assert [resume.id for resume in resumes] == [second.id, first.id]
    assert all(resume.user_id == user.id for resume in resumes)


def test_cv_11_list_filters_by_status(db: Session) -> None:
    user = _user(db)
    ready = _add_resumes(db, user, 2, ResumeStatus.READY)
    _add_resumes(db, user, 1, ResumeStatus.RECEIVED)
    _add_resumes(db, user, 1, ResumeStatus.FAILED)
    other = _user(db)
    _add_resumes(db, other, 1, ResumeStatus.READY)

    resumes = list_resumes(db, user, ResumeStatus.READY)

    assert {resume.id for resume in resumes} == {resume.id for resume in ready}


def test_cv_11_list_accepts_status_value_string(db: Session) -> None:
    user = _user(db)
    ready = _add_resumes(db, user, 1, ResumeStatus.READY)
    _add_resumes(db, user, 1, ResumeStatus.FAILED)

    resumes = list_resumes(db, user, "ready")  # type: ignore[arg-type]

    assert [resume.id for resume in resumes] == [ready[0].id]


@pytest.mark.parametrize("status", ["unknown", "READY", 1, "\x00", "\ud800"])
def test_cv_11_list_rejects_unknown_status(db: Session, status: object) -> None:
    user = _user(db)

    with pytest.raises(AppError) as exc_info:
        list_resumes(db, user, status)  # type: ignore[arg-type]

    assert exc_info.value.code == VALIDATION_ERROR


def test_cv_13_list_is_empty_without_versions(db: Session) -> None:
    assert list_resumes(db, _user(db)) == []


# --- get_owned_resume -------------------------------------------------------------------


def test_get_owned_resume_returns_own_version(db: Session) -> None:
    user = _user(db)
    resume = _add_resumes(db, user, 1, ResumeStatus.READY)[0]

    assert get_owned_resume(db, user, resume.id).id == resume.id
    assert get_owned_resume(db, user, resume.id, for_update=True).id == resume.id


def test_cv_13_get_owned_resume_of_other_user_is_not_found(db: Session) -> None:
    owner = _user(db)
    resume = _add_resumes(db, owner, 1, ResumeStatus.READY)[0]
    intruder = _user(db)

    with pytest.raises(AppError) as exc_info:
        get_owned_resume(db, intruder, resume.id)

    assert exc_info.value.code == RESOURCE_NOT_FOUND
    assert exc_info.value.status == 404


def test_get_owned_resume_unknown_id_is_not_found(db: Session) -> None:
    with pytest.raises(AppError) as exc_info:
        get_owned_resume(db, _user(db), uuid.uuid4(), for_update=True)

    assert exc_info.value.code == RESOURCE_NOT_FOUND


@pytest.mark.parametrize("resume_id", ["not-a-uuid", None, 1, "\x00"])
def test_get_owned_resume_invalid_id_is_not_found(db: Session, resume_id: object) -> None:
    with pytest.raises(AppError) as exc_info:
        get_owned_resume(db, _user(db), resume_id)  # type: ignore[arg-type]

    assert exc_info.value.code == RESOURCE_NOT_FOUND
