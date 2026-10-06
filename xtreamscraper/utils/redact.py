"""Credential redaction for anything that may reach logs, the database or the WebUI.

Xtream embeds the username and password in every API and stream URL, so any URL,
exception message or traceback can leak them. Everything user-visible goes through
:func:`redact`.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Final, Iterable, Optional

log = logging.getLogger(__name__)

MASK: Final = "***"

# Known secret values (provider passwords, and usernames because they appear next to
# the password in stream URLs). Registered by the provider repository.
_secrets: set[str] = set()
_lock = threading.Lock()

# /movie/<user>/<pass>/<id>.ext, /series/<user>/<pass>/..., /live/<user>/<pass>/...
_PATH_CREDENTIALS = re.compile(
    r"(/(?:movie|series|live|timeshift)/)([^/\s?#]+)/([^/\s?#]+)(/)", re.IGNORECASE
)
#: Parameter / field names whose value is always a credential.
_SECRET_KEYS = (r"username|password|passwd|passphrase|pwd|user|pass|api[_-]?key|access[_-]?token"
                r"|refresh[_-]?token|auth[_-]?token|read[_-]?token|id[_-]?token|client[_-]?secret|secret|token")
# username=...&password=... in query strings or form bodies
_QUERY_CREDENTIALS = re.compile(rf"((?:{_SECRET_KEYS})=)([^&\s#'\"]+)", re.IGNORECASE)
# "password": "...", 'api_key': '...', password = "..." (JSON, Python reprs, config dumps)
_QUOTED_CREDENTIALS = re.compile(
    rf"""((?:{_SECRET_KEYS})["']?\s*[:=]\s*)(["'])((?:\\.|(?!\2)[^\\])+)\2""", re.IGNORECASE
)
# Authorization: Bearer <token>
_BEARER = re.compile(r"(\bBearer\s+)([A-Za-z0-9._~+/=-]+)", re.IGNORECASE)
# Authorization: Basic <base64> / Authorization: <token> (header dumps)
_AUTHORIZATION = re.compile(r"""(\bAuthorization["']?\s*[:=]\s*["']?(?:Basic\s+|Bearer\s+|Token\s+)?)([^\s"',;]+)""",
                            re.IGNORECASE)
# scheme://user:pass@host
_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@", re.IGNORECASE)

#: Shorter registered values are not masked in free text: replacing every "ab" in a message
#: would wreck the logs, and the pattern of masks would itself reveal the value. The
#: structured patterns above still mask them in URLs, query strings and JSON.
MIN_SECRET_LENGTH: Final = 3
_short_warned: set[str] = set()


def register_secret(value: Optional[str]) -> None:
    """Remember a secret so :func:`redact` masks it wherever it appears.

    Contract:
        Silences:
            - A value shorter than ``MIN_SECRET_LENGTH`` is not registered: it is only masked
              where a structured pattern (URL path, query, JSON, header) catches it. A warning
              is logged once per value. Empty values are ignored. Registered values stay for
              the life of the process.
    """
    if not value:
        return
    if len(value) < MIN_SECRET_LENGTH:
        with _lock:
            first = value not in _short_warned
            _short_warned.add(value)
        if first:  # logged outside the lock: log handlers call redact()
            log.warning("A credential shorter than %d characters cannot be masked everywhere in logs, "
                        "only in URLs, query strings and JSON; use a longer value", MIN_SECRET_LENGTH)
        return
    with _lock:
        _secrets.add(value)


def register_secrets(values: Iterable[Optional[str]]) -> None:
    for value in values:
        register_secret(value)


def redact(text: object, extra_secrets: Iterable[Optional[str]] = ()) -> str:
    """Return ``text`` with credentials removed. Never raises.

    Contract:
        Preconditions:
            - Free-text masking covers only registered secrets and ``extra_secrets`` of at
              least ``MIN_SECRET_LENGTH`` characters; shorter ones are dropped silently.

        Silences:
            - An object whose ``__str__`` raises gives ``"<unprintable>"``; ``None`` gives ``""``.

        Over-masks (fails safe):
            - Any ``/movie|series|live|timeshift/<a>/<b>/`` path segment pair is masked, also
              in library paths (``/Series/Drama/Show/`` gives ``/Series/***/***/``), and every
              occurrence of a registered secret is masked, also inside unrelated words.
            - A JSON number value (``"password": 12345``) is not caught by the structured
              patterns; only the registered-secret pass masks it.
    """
    if text is None:
        return ""
    try:
        result = str(text)
    except Exception:  # pragma: no cover - defensive
        return "<unprintable>"
    result = _PATH_CREDENTIALS.sub(lambda m: f"{m.group(1)}{MASK}/{MASK}{m.group(4)}", result)
    result = _QUERY_CREDENTIALS.sub(lambda m: f"{m.group(1)}{MASK}", result)
    result = _QUOTED_CREDENTIALS.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}{m.group(2)}", result)
    result = _AUTHORIZATION.sub(lambda m: f"{m.group(1)}{MASK}", result)
    result = _USERINFO.sub(lambda m: f"{m.group(1)}{MASK}:{MASK}@", result)
    result = _BEARER.sub(lambda m: f"{m.group(1)}{MASK}", result)
    with _lock:
        secrets = set(_secrets)
    secrets.update(s for s in extra_secrets if s and len(s) >= MIN_SECRET_LENGTH)
    # Longest first so a secret containing another secret is fully masked.
    for secret in sorted(secrets, key=len, reverse=True):
        if secret in result:
            result = result.replace(secret, MASK)
    return result


def redact_url(url: Optional[str]) -> str:
    return redact(url or "")
