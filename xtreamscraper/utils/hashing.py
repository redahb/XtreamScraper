"""Fingerprints for change detection. Stream URLs are only ever stored as hashes."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def url_fingerprint(url: str) -> str:
    """Hash of a (credential-bearing) stream URL, safe to persist."""
    return sha256_text(url.strip())


def fingerprint(*parts: Any) -> str:
    """Stable hash over a list of JSON-serialisable values."""
    payload = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return sha256_text(payload)[:32]
