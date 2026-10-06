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
_RESERVED_STEM = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³]|CONIN\$|CONOUT\$)$", re.IGNORECASE)
_SPACES = re.compile(r"\s+")
_UNDERSCORES = re.compile(r"_{2,}")


def sanitize_component(name: object, max_length: int = DEFAULT_MAX_LENGTH, fallback: str = UNKNOWN) -> str:
    """Make ``name`` a valid single Windows path component.

    * ``Title: Subtitle`` becomes ``Title - Subtitle``; other invalid characters become ``_``
    * control characters are removed, whitespace is collapsed
    * trailing dots and spaces are stripped (Windows silently drops them)
    * a reserved device name before the first dot (``CON``, ``NUL.mkv``, ``Con. Air``)
      gets a ``_`` right after it (``CON_``, ``NUL_.mkv``, ``Con_. Air``)
    * the result is truncated to ``max_length`` characters
    * an empty result becomes ``fallback``

    Contract:
        Preconditions:
            - ``max_length`` must be at least 1. With 0 the text is dropped and the fallback
              comes back uncut; a negative value cuts from the end (``text[:-2]``).
            - ``fallback`` is used as given: it is neither sanitized nor cut to
              ``max_length`` (only a reserved device name in it is neutralised). An empty
              fallback gives ``""``.

        Silences:
            - Any falsy ``name`` (``None``, ``0``, ``""``) gives the fallback; other objects
              go through ``str()``.
    """
    text = unicodedata.normalize("NFC", str(name or ""))
    text = text.replace(": ", " - ").replace(":", "-")
    # Drop control characters before collapsing whitespace, so a removed one cannot leave two
    # spaces behind; whitespace controls (tab, CR, LF) are kept and collapse to one space.
    text = "".join(ch for ch in text if ch.isspace() or unicodedata.category(ch) not in ("Cc", "Cf"))
    text = _SPACES.sub(" ", text)
    text = _INVALID_CHARS.sub("_", text)
    text = _UNDERSCORES.sub("_", text)
    text = text.strip(" ._-")
    if len(text) > max_length:
        text = text[:max_length].rstrip(" ._-")
    text = text.rstrip(". ")
    if not text:
        text = fallback
    return _neutralise_reserved(text, max_length)


def _neutralise_reserved(text: str, max_length: int) -> str:
    """Put ``_`` after a reserved device-name stem (the part before the first dot).

    Windows treats ``NUL.mkv`` and ``CON .txt`` as the device too, so the suffix must go on
    the stem, not at the end. The result stays within ``max_length`` and never matches
    ``_RESERVED``; re-sanitising it returns it unchanged.
    """
    stem, dot, rest = text.partition(".")
    bare = stem.rstrip(" ")
    if not _RESERVED_STEM.match(bare):
        return text
    tail = stem[len(bare):] + dot + rest
    room = max_length - len(bare) - 1
    if room >= 0:
        # Truncate only the tail so the protecting "_" can never be cut off.
        tail = tail[:room].rstrip(" ._-")
        return f"{bare}_{tail}"
    # Pathologically short limit: the "_" goes inside the stem instead.
    return (bare[:1] + "_" + bare[1:])[:max(max_length, 1)]


def is_valid_component(name: str) -> bool:
    return bool(name) and name == sanitize_component(name, max_length=max(len(name), 1))
