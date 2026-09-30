"""Unit tests for private resume file storage (CV-01, DATA-04, DA-14)."""

import stat
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from app.config import get_settings
from app.resumes.storage import delete_file, delete_user_files, read_file, save_file


@pytest.fixture(autouse=True)
def _storage_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    storage = tmp_path / "storage"
    monkeypatch.setenv("IR_THROTTLE_SECRET", "test-throttle-secret")
    monkeypatch.setenv("IR_OIDC_STATE_SECRET", "test-oidc-state-secret")
    monkeypatch.setenv("IR_STORAGE_DIR", str(storage))
    get_settings.cache_clear()
    yield storage
    get_settings.cache_clear()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_cv_01_save_then_read_returns_same_bytes_with_0600(_storage_dir: Path) -> None:
    user_id, resume_id = uuid4(), uuid4()
    data = b"%PDF-1.4 resume bytes"

    key = save_file(user_id, resume_id, data)

    assert read_file(key) == data
    path = _storage_dir / str(user_id) / f"{resume_id}.pdf"
    assert path.is_file()
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    assert _mode(_storage_dir) == 0o700


def test_cv_01_key_is_relative_user_and_resume_path() -> None:
    user_id, resume_id = uuid4(), uuid4()

    key = save_file(user_id, resume_id, b"data")

    assert key == f"{user_id}/{resume_id}.pdf"


def test_cv_01_save_overwrites_existing_file() -> None:
    user_id, resume_id = uuid4(), uuid4()
    save_file(user_id, resume_id, b"old")

    key = save_file(user_id, resume_id, b"new")

    assert read_file(key) == b"new"


def test_data_04_delete_file_twice_is_idempotent(_storage_dir: Path) -> None:
    user_id, resume_id = uuid4(), uuid4()
    key = save_file(user_id, resume_id, b"data")

    delete_file(key)
    delete_file(key)

    assert not (_storage_dir / key).exists()


@pytest.mark.parametrize("key", ["../x", "a/../../x", "/etc/passwd", "", "a/./b.pdf"])
def test_cv_01_read_file_rejects_unsafe_key(key: str) -> None:
    with pytest.raises(ValueError):
        read_file(key)


def test_data_04_delete_file_rejects_traversal_key() -> None:
    with pytest.raises(ValueError):
        delete_file("../x")


def test_data_04_delete_user_files_removes_user_directory(_storage_dir: Path) -> None:
    user_id = uuid4()
    other_user = uuid4()
    save_file(user_id, uuid4(), b"a")
    save_file(user_id, uuid4(), b"b")
    other_key = save_file(other_user, uuid4(), b"c")

    delete_user_files(user_id)

    assert not (_storage_dir / str(user_id)).exists()
    assert read_file(other_key) == b"c"


def test_data_04_delete_user_files_without_directory_is_noop() -> None:
    delete_user_files(uuid4())
