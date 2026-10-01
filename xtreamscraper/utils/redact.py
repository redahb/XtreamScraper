"""Credential redaction for anything that may reach logs, the database or the WebUI.

Xtream embeds the username and password in every API and stream URL, so any URL,
exception message or traceback can leak them. Everything user-visible goes through
:func:`redact`.
"""

from __future__ import annotations

import re
import threading
from typing import Iterable, Optional

MASK = "***"

# Known secret values (provider passwords, and usernames because they appear next to
# the password in stream URLs). Registered by the provider repository.
_secrets: set[str] = set()
_lock = threading.Lock()

# /movie/<user>/<pass>/<id>.ext, /series/<user>/<pass>/..., /live/<user>/<pass>/...
_PATH_CREDENTIALS = re.compile(
    r"(/(?:movie|series|live|timeshift)/)([^/\s?#]+)/([^/\s?#]+)(/)", re.IGNORECASE
)
# username=...&password=... in query strings or form bodies
_QUERY_CREDENTIALS = re.compile(
    r"((?:username|password|user|pass|api_key|apikey|access_token|token)=)([^&\s#'\"]+)", re.IGNORECASE
)
# Authorization: Bearer <token>
_BEARER = re.compile(r"(\bBearer\s+)([A-Za-z0-9._~+/=-]+)", re.IGNORECASE)
# scheme://user:pass@host
_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@", re.IGNORECASE)


def register_secret(value: Optional[str]) -> None:
    """Remember a secret so :func:`redact` masks it wherever it appears."""
    if value and len(value) >= 3:
        with _lock:
            _secrets.add(value)


def register_secrets(values: Iterable[Optional[str]]) -> None:
    for value in values:
        register_secret(value)


def redact(text: object, extra_secrets: Iterable[Optional[str]] = ()) -> str:
    """Return ``text`` with credentials removed. Never raises."""
    if text is None:
        return ""
    try:
        result = str(text)
    except Exception:  # pragma: no cover - defensive
        return "<unprintable>"
    result = _PATH_CREDENTIALS.sub(lambda m: f"{m.group(1)}{MASK}/{MASK}{m.group(4)}", result)
    result = _QUERY_CREDENTIALS.sub(lambda m: f"{m.group(1)}{MASK}", result)
    result = _USERINFO.sub(lambda m: f"{m.group(1)}{MASK}:{MASK}@", result)
    result = _BEARER.sub(lambda m: f"{m.group(1)}{MASK}", result)
    with _lock:
        secrets = set(_secrets)
    secrets.update(s for s in extra_secrets if s and len(s) >= 3)
    # Longest first so a secret containing another secret is fully masked.
    for secret in sorted(secrets, key=len, reverse=True):
        if secret in result:
            result = result.replace(secret, MASK)
    return result


def redact_url(url: Optional[str]) -> str:
    return redact(url or "")
