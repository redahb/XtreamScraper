"""Windows-safe file and folder name sanitisation.

Applied to every path component the application creates (provider, category, movie,
series, season and episode names), regardless of the OS it runs on, so a library
built anywhere can be used from Windows.
"""

from __future__ import annotations

import re
import unicodedata

UNKNOWN = "Unknown"
DEFAULT_MAX_LENGTH = 120

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³]|CONIN\$|CONOUT\$)(\..*)?$", re.IGNORECASE)
_SPACES = re.compile(r"\s+")
_UNDERSCORES = re.compile(r"_{2,}")


def sanitize_component(name: object, max_length: int = DEFAULT_MAX_LENGTH, fallback: str = UNKNOWN) -> str:
    """Make ``name`` a valid single Windows path component.

    * ``Title: Subtitle`` becomes ``Title - Subtitle``; other invalid characters become ``_``
    * control characters are removed, whitespace is collapsed
    * trailing dots and spaces are stripped (Windows silently drops them)
    * reserved device names (``CON``, ``NUL``, ``COM1`` ...) get a ``_`` suffix
    * the result is truncated to ``max_length`` characters
    * an empty result becomes ``fallback``
    """
    text = unicodedata.normalize("NFC", str(name or ""))
    text = text.replace(": ", " - ").replace(":", "-")
    text = _SPACES.sub(" ", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))
    text = _INVALID_CHARS.sub("_", text)
    text = _UNDERSCORES.sub("_", text)
    text = text.strip(" ._-")
    if len(text) > max_length:
        text = text[:max_length].rstrip(" ._-")
    text = text.rstrip(". ")
    if not text:
        text = fallback
    if _RESERVED.match(text):
        text = f"{text}_"
    return text


def is_valid_component(name: str) -> bool:
    return bool(name) and name == sanitize_component(name, max_length=max(len(name), 1))
