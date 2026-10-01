"""Private on-disk storage for resume files (CV-01, DATA-04, DA-14).

Files live at ``storage_dir/<user_id>/<resume_id>.pdf`` with directories set to
0700 and files to 0600. The storage directory must never be exposed by a static
route. Keys are produced by :func:`save_file` and never accepted from clients;
every key is still validated before touching the filesystem.
"""

import os
import re
import shutil
from pathlib import Path
from uuid import UUID

from app.config import get_settings

DIR_MODE = 0o700
FILE_MODE = 0o600

_UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_KEY_RE = re.compile(rf"(?P<user>{_UUID_PATTERN})/(?P<resume>{_UUID_PATTERN})\.pdf")


def _root() -> Path:
    return Path(get_settings().storage_dir).resolve()


def _ensure_dir(path: Path) -> None:
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    # mkdir honours the umask; enforce the mode explicitly.
    os.chmod(path, DIR_MODE)


def _path_for_key(key: str) -> Path:
    """Map a storage key to an absolute path inside the storage root.

    Raises ``ValueError`` for anything other than ``<uuid>/<uuid>.pdf``, which
    rules out absolute paths, ``..`` segments and other traversal tricks.
    """
    if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
        raise ValueError("invalid storage key")
    root = _root()
    path = (root / key).resolve()
    if not path.is_relative_to(root):
        raise ValueError("invalid storage key")
    return path


def save_file(user_id: UUID, resume_id: UUID, data: bytes) -> str:
    """Store ``data`` privately and return its storage key."""
    key = f"{UUID(str(user_id))}/{UUID(str(resume_id))}.pdf"
    path = _path_for_key(key)
    root = _root()
    _ensure_dir(root)
    _ensure_dir(path.parent)

    tmp_path = path.with_name(f".{path.name}.tmp")
    fd = os.open(
        tmp_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        FILE_MODE,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, FILE_MODE)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return key


def read_file(key: str) -> bytes:
    """Return the bytes stored under ``key``."""
    path = _path_for_key(key)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def delete_file(key: str) -> None:
    """Delete the file stored under ``key``; a missing file is not an error."""
    path = _path_for_key(key)
    path.unlink(missing_ok=True)


def delete_user_files(user_id: UUID) -> None:
    """Delete every stored file of ``user_id``; a missing directory is a no-op."""
    root = _root()
    user_dir = root / str(UUID(str(user_id)))
    if user_dir.is_symlink():
        user_dir.unlink()
        return
    if not user_dir.resolve().is_relative_to(root):
        raise ValueError("invalid user directory")
    if user_dir.exists():
        shutil.rmtree(user_dir)
