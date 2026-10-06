"""Defensive coercion of decoded JSON values (provider and scraper responses).

Remote data can change shape at any time; these helpers never raise and return an empty
or ``None`` value for anything of the wrong type, so mapping code can chain lookups
(``as_dict(as_dict(data).get("video")).get("streams")``) without guarding every step.
"""

from __future__ import annotations

import math
from typing import Any, Optional


def as_dict(value: Any) -> dict:
    """``value`` when it is a dict, else an empty dict."""
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list:
    """``value`` when it is a list, else an empty list."""
    return value if isinstance(value, list) else []


def dict_items(value: Any) -> list[dict]:
    """The dict entries of ``value`` when it is a list (other entries dropped), else ``[]``."""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def as_text(value: Any) -> Optional[str]:
    """A stripped, non-empty string; ``None`` for anything else (numbers included)."""
    return value.strip() or None if isinstance(value, str) else None


def scalar_text(value: Any) -> Optional[str]:
    """Strings and numbers as stripped, non-empty text; ``None`` for ``None``, booleans,
    dicts, lists and empty values."""
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    return text or None


def as_int(value: Any) -> Optional[int]:
    """``value`` when it is an int (booleans excluded), else ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def minutes_from_seconds(seconds: Any) -> Optional[int]:
    """Whole minutes from a duration in seconds; ``None`` below one minute or when not a number."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) \
            or seconds < 60:
        return None
    return int(seconds) // 60
