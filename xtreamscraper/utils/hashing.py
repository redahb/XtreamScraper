"""Fingerprints for change detection. Stream URLs are only ever stored as hashes."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from ..filesystem import atomic


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: str) -> Optional[str]:
    """SHA-256 of a file's bytes, or None when it cannot be read.

    Contract:
        Silences:
            - Every ``OSError`` from opening or reading ``path`` (missing file, permission
              denied, a directory, a locked file on Windows) becomes ``None``. The caller
              cannot tell "missing" from "unreadable".
    """
    try:
        return hashlib.sha256(atomic.read_bytes(path)).hexdigest()
    except OSError:
        return None


def url_fingerprint(url: str) -> str:
    """Hash of a (credential-bearing) stream URL, safe to persist."""
    return sha256_text(url.strip())


def fingerprint(*parts: Any) -> str:
    """Stable hash over a list of JSON-serialisable values (32 hex characters).

    The result is persisted (e.g. ``source_fp`` in the sync tables), so it must stay the same
    across releases for the same inputs.

    Contract:
        Preconditions:
            - Each part must keep its type across releases. ``1``, ``1.0`` and ``"1"`` give
              three different hashes, so changing a part's type (a year passed as ``str``
              instead of ``int``) silently changes every stored fingerprint.
            - Parts should be JSON values (str, int, float, bool, None, lists and dicts with
              str keys). Anything else is hashed through ``str()`` (``default=str``), which is
              only stable when that ``__str__`` is.

        Raises:
            TypeError: For dicts whose keys cannot be sorted against each other (implicit,
                from ``json.dumps(sort_keys=True)``).
            ValueError: For a circular structure (implicit, from ``json.dumps``).
    """
    payload = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return sha256_text(payload)[:32]
