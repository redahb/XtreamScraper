"""Safe file I/O: UTF-8, atomic replace, Windows long-path support."""

from __future__ import annotations

import logging
import os
import tempfile
import time

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
_LONG_PATH_THRESHOLD = 240


def _read_umask() -> int:
    """The process umask. Read once at import, before any worker thread exists: the only portable
    way to read it is to set it, which briefly affects files other threads create."""
    if IS_WINDOWS:
        return 0
    mask = os.umask(0)
    os.umask(mask)
    return mask


# mkstemp creates owner-only (0600) files; a library file gets the mode a plain open() would give
# it, so a media server running as another user can read it.
_NEW_FILE_MODE = 0o666 & ~_read_umask()


def fs_path(path: str) -> str:
    r"""Path usable by the OS. Long Windows paths get the ``\\?\`` prefix (bypasses MAX_PATH).

    Contract:
        Preconditions:
            - On POSIX ``path`` is returned unchanged (a relative path stays relative). On
              Windows it is made absolute against the current directory, and gets the prefix
              (``\\?\UNC\`` for a UNC path) only from 240 characters on.
    """
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
    """The file as UTF-8 text (a BOM is dropped), at most ``max_bytes`` bytes of it.

    Contract:
        Raises:
            OSError: The file is missing, a folder or unreadable (from ``open``, not caught).

        Silences:
            - A longer file is cut at ``max_bytes`` without notice, possibly inside a character.
            - Invalid UTF-8 becomes U+FFFD instead of raising ``UnicodeDecodeError``.
    """
    with open(fs_path(path), "rb") as handle:
        data = handle.read(max_bytes)
    return data.decode("utf-8-sig", errors="replace")


def read_bytes(path: str) -> bytes:
    with open(fs_path(path), "rb") as handle:
        return handle.read()


def _replace(src: str, dst: str, attempts: int = 5) -> None:
    """``os.replace(src, dst)``, retried while the target is locked.

    On Windows a reader (media server, antivirus) can briefly lock the target file.

    Contract:
        Preconditions:
            - ``attempts`` must be at least 1. With 0 or less nothing is replaced and no
              error is raised.

        Raises:
            PermissionError: Still locked after ``attempts`` tries (sleeps 0.2 s, 0.4 s, ...
                in between, about 2 s in total for the default 5).
            OSError: Any other failure, on the first try (not retried).
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def _widen_mode(fd: int) -> None:
    if IS_WINDOWS or not hasattr(os, "fchmod"):
        return  # Windows: ACLs are inherited from the folder
    try:
        os.fchmod(fd, _NEW_FILE_MODE)
    except OSError as exc:  # e.g. a mount that does not support modes: keep mkstemp's mode
        log.debug("Could not set the mode of a new file: %s", exc)


def atomic_write_bytes(path: str, data: bytes) -> None:
    """Write via a temp file in the same directory, fsync, then atomically replace.

    Readers see either the old file or the complete new one, never a partial write. Missing
    parent folders are created. On POSIX the file gets the mode a plain ``open()`` would give
    a new file (``0o666`` minus the umask read at import), also when it replaces an existing
    file with another mode.

    Contract:
        Raises:
            OSError: Creating the folder or the temp file, writing, fsync or the replace
                failed (disk full, permission denied, target is a folder, still locked after
                the ``_replace`` retries). The target is then unchanged and the temp file is
                removed; any exception, ``KeyboardInterrupt`` included, is re-raised.

        Silences:
            - An ``OSError`` while removing the temp file after a failure: a
              ``.tmp-*.part`` file may stay behind (nothing sweeps it later).
            - An ``OSError`` from setting the mode (logged at debug level); the file then
              keeps ``mkstemp``'s owner-only mode.
    """
    target = fs_path(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=directory)
    try:
        _widen_mode(fd)
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
    """Rename ``src`` to ``dst`` if ``src`` exists and ``dst`` does not. Never overwrites.

    Missing parent folders of ``dst`` are created. A case-only rename (``Heat`` to ``heat``)
    on a case-insensitive file system goes through ``<dst>.renaming``; if its second step
    fails the file is renamed back before the error is raised.

    Contract:
        Preconditions:
            - No other thread or process creates ``dst`` at the same time. On POSIX
              ``os.rename`` replaces an existing target, so "never overwrites" is a check
              followed by the rename and relies on the per-provider job locks. On Windows
              ``os.rename`` itself refuses an existing target.

        Raises:
            OSError: From creating the folder or from the rename (e.g. the file is locked,
                or ``dst`` appeared after the check, on Windows). Not caught.

        Silences:
            - Returns ``False``, with no reason given, when ``src`` is missing, ``src`` and
              ``dst`` are the same path, or ``dst`` exists.
            - A failed rename back after a failed case-only rename is only logged; the file
              then stays at ``<dst>.renaming`` and the first error is raised.
            - ``os.path.normcase`` folds case only on Windows, so on a case-insensitive
              macOS volume a case-only rename finds ``dst`` "existing" and returns ``False``.
    """
    source, target = fs_path(src), fs_path(dst)
    if not os.path.exists(source):
        return False
    if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(target)):
        if source == target:
            return False
        # Case-only rename on a case-insensitive filesystem: go through a temp name.
        interim = target + ".renaming"
        os.rename(source, interim)
        try:
            os.rename(interim, target)
        except OSError:
            # Put the file back under its old name rather than leave it as "<name>.renaming".
            try:
                os.rename(interim, source)
            except OSError as exc:
                log.warning("Could not restore %s after a failed rename: %s", interim, exc)
            raise
        return True
    if os.path.exists(target):
        return False
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.rename(source, target)
    return True
