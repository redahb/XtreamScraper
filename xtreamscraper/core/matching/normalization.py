"""Title normalization for matching (never used for file names).

Goal: make superficial differences disappear (case, accents, punctuation style, spacing,
a trailing ``(YEAR)``) while keeping everything that distinguishes titles: words, numbers
and meaningful symbols such as ``+`` and ``#``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

# "Title (2021)" / "Title [2021]" / "Title - 2021" at the very end.
_TRAILING_YEAR = re.compile(r"[\s\-–—:]*[\(\[]\s*((?:18|19|20)\d{2})\s*[\)\]]\s*$|\s+[-–—]\s+((?:18|19|20)\d{2})\s*$")
# Apostrophes join words ("Schindler's" -> "schindlers").
_APOSTROPHES = re.compile(r"['’‘`´]")
# Separators become spaces.
_SEPARATORS = re.compile(r"[\s\-–—_:;,./\\|·•~]+")
# Everything that is not a letter, digit, space or a meaningful symbol is dropped.
_DROP = re.compile(r"[^\w\s+#]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def strip_trailing_year(title: str, year: Optional[int]) -> str:
    """Remove a trailing year notation when the year is known separately."""
    if year is None:
        return title
    stripped = _TRAILING_YEAR.sub("", title)
    return stripped if stripped.strip() else title


def _fold_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_title(title: Optional[str], year: Optional[int] = None) -> str:
    """Comparable form of a title.

    ``"Amélie (2001)", 2001`` -> ``"amelie"``; ``"Mission: Impossible – Fallout"`` ->
    ``"mission impossible fallout"``; ``"Schindler's List"`` -> ``"schindlers list"``.
    """
    text = unicodedata.normalize("NFKC", title or "").strip()
    text = strip_trailing_year(text, year)
    text = _fold_accents(text).casefold()
    text = text.replace("&", " and ")
    text = _APOSTROPHES.sub("", text)
    text = _SEPARATORS.sub(" ", text)
    text = _DROP.sub("", text).replace("_", " ")
    return _SPACES.sub(" ", text).strip()
