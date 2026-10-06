"""Reading and writing ``.strm`` files (one stream URL per file)."""

from __future__ import annotations

import enum
import logging
from urllib.parse import urlsplit

from . import atomic

log = logging.getLogger(__name__)

STRM_EXTENSION = ".strm"


class WriteResult(enum.Enum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


def normalize_url(text: str | None) -> str:
    return (text or "").strip()


def read_strm_url(path: str) -> str | None:
    """First non-empty, non-comment line of the file, or None when absent/unreadable/empty.

    Contract:
        Silences:
            - Any ``OSError`` (missing file, folder, no access) gives ``None``, the same
              answer as an empty file. Only the first 64 KiB are read.
            - The line is returned as found; it is not checked to be a URL
              (see ``is_usable_url``).
    """
    try:
        content = atomic.read_text(path, max_bytes=64 * 1024)
    except OSError:
        return None
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def is_usable_url(url: str | None) -> bool:
    """True for an http(s), rtmp, rtsp, rtp or udp URL with a host and no whitespace.

    Contract:
        Silences:
            - A ``ValueError`` from ``urlsplit`` (e.g. a malformed IPv6 host) gives ``False``.
    """
    if not url or any(c.isspace() for c in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https", "rtmp", "rtsp", "rtp", "udp") and bool(parts.netloc)


def write_strm(path: str, url: str) -> WriteResult:
    """Create or update the STRM so it holds exactly ``url``; untouched when already correct.

    The file holds the URL stripped of surrounding whitespace plus one newline.

    Contract:
        Preconditions:
            - The caller owns ``path``: a different existing file there is overwritten.

        Raises:
            OSError: From the atomic write (see ``atomic_write_bytes``), not caught.

        Silences:
            - An ``OSError`` while reading the current file is treated as "different", and
              the file is rewritten (``UPDATED``).
    """
    desired = normalize_url(url)
    if atomic.is_file(path):
        try:
            current = atomic.read_text(path, max_bytes=64 * 1024)
        except OSError:
            current = None
        if current is not None and normalize_url(current) == desired:
            return WriteResult.UNCHANGED
        atomic.atomic_write_text(path, desired + "\n")
        return WriteResult.UPDATED
    atomic.atomic_write_text(path, desired + "\n")
    return WriteResult.CREATED
