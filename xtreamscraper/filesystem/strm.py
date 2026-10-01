"""Reading and writing ``.strm`` files (one stream URL per file)."""

from __future__ import annotations

import enum
import logging
from typing import Optional
from urllib.parse import urlsplit

from . import atomic

log = logging.getLogger(__name__)

STRM_EXTENSION = ".strm"


class WriteResult(enum.Enum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


def normalize_url(text: str) -> str:
    return (text or "").strip()


def read_strm_url(path: str) -> Optional[str]:
    """First non-empty, non-comment line of the file, or None when absent/unreadable/empty."""
    try:
        content = atomic.read_text(path, max_bytes=64 * 1024)
    except OSError:
        return None
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def is_usable_url(url: Optional[str]) -> bool:
    if not url or any(c.isspace() for c in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https", "rtmp", "rtsp", "rtp", "udp") and bool(parts.netloc)


def write_strm(path: str, url: str) -> WriteResult:
    """Create or update the STRM so it holds exactly ``url``; untouched when already correct."""
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
