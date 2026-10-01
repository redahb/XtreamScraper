"""Safe file I/O: UTF-8, atomic replace, Windows long-path support."""

from __future__ import annotations

import logging
import os
import tempfile
import time

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
_LONG_PATH_THRESHOLD = 240


def fs_path(path: str) -> str:
    r"""Path usable by the OS. Long Windows paths get the ``\\?\`` prefix (bypasses MAX_PATH)."""
    if not IS_WINDOWS:
        return path
    absolute = os.path.abspath(path)
    if len(absolute) < _LONG_PATH_THRESHOLD or absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def exists(path: str) -> bool:
    return os.path.exists(fs_path(path))


def is_file(path: str) -> bool:
    return os.path.isfile(fs_path(path))


def is_dir(path: str) -> bool:
    return os.path.isdir(fs_path(path))


def ensure_dir(path: str) -> None:
    os.makedirs(fs_path(path), exist_ok=True)


def read_text(path: str, max_bytes: int = 4 * 1024 * 1024) -> str:
    with open(fs_path(path), "rb") as handle:
        data = handle.read(max_bytes)
    return data.decode("utf-8-sig", errors="replace")


def read_bytes(path: str) -> bytes:
    with open(fs_path(path), "rb") as handle:
        return handle.read()


def _replace(src: str, dst: str, attempts: int = 5) -> None:
    # On Windows a reader (media server, antivirus) can briefly lock the target file.
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def atomic_write_bytes(path: str, data: bytes) -> None:
    """Write via a temp file in the same directory, fsync, then atomically replace."""
    target = fs_path(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: str, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def move(src: str, dst: str) -> bool:
    """Rename ``src`` to ``dst`` if ``src`` exists and ``dst`` does not. Never overwrites."""
    source, target = fs_path(src), fs_path(dst)
    if not os.path.exists(source):
        return False
    if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(target)):
        if source == target:
            return False
        # Case-only rename on a case-insensitive filesystem: go through a temp name.
        interim = target + ".renaming"
        os.rename(source, interim)
        os.rename(interim, target)
        return True
    if os.path.exists(target):
        return False
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.rename(source, target)
    return True
