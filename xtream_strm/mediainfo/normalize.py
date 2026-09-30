"""Value normalization shared by every media-information source and by the NFO parser.

Everything here is conservative: a value that is not clearly meaningful becomes ``None``
instead of a zero, a placeholder or a guess.
"""

from __future__ import annotations

import math
import re
from fractions import Fraction
from typing import Any, Iterable, Optional

from .models import APPROVED_FLAGS, FORMAT_3D, HDR_TYPES, SCAN_TYPES

_INVALID_TEXT = {"", "n/a", "na", "none", "null", "unknown", "0/0", "0:0"}

# Known compatibility aliases only; everything else stays as the source named it.
_CODEC_ALIASES = {
    "subrip": "srt",
    "hdmv_pgs_subtitle": "pgssub",
    "dvd_subtitle": "dvdsub",
    "dca": "dts",
}
_XVID_TAGS = {"xvid"}
_DIVX_TAGS = {"divx", "dx50", "div3", "div4", "div5", "div6"}

# ISO 639-1 -> ISO 639-2 (bibliographic form, the one Matroska and ffprobe report).
_ISO639_1_TO_2 = {
    "aa": "aar", "af": "afr", "am": "amh", "ar": "ara", "as": "asm", "az": "aze", "be": "bel",
    "bg": "bul", "bn": "ben", "bo": "tib", "bs": "bos", "ca": "cat", "cs": "cze", "cy": "wel",
    "da": "dan", "de": "ger", "el": "gre", "en": "eng", "eo": "epo", "es": "spa", "et": "est",
    "eu": "baq", "fa": "per", "fi": "fin", "fo": "fao", "fr": "fre", "ga": "gle", "gd": "gla",
    "gl": "glg", "gu": "guj", "he": "heb", "hi": "hin", "hr": "hrv", "hu": "hun", "hy": "arm",
    "id": "ind", "is": "ice", "it": "ita", "ja": "jpn", "jv": "jav", "ka": "geo", "kk": "kaz",
    "km": "khm", "kn": "kan", "ko": "kor", "ku": "kur", "la": "lat", "lb": "ltz", "lo": "lao",
    "lt": "lit", "lv": "lav", "mk": "mac", "ml": "mal", "mn": "mon", "mr": "mar", "ms": "may",
    "mt": "mlt", "my": "bur", "nb": "nob", "ne": "nep", "nl": "dut", "nn": "nno", "no": "nor",
    "pa": "pan", "pl": "pol", "ps": "pus", "pt": "por", "ro": "rum", "ru": "rus", "si": "sin",
    "sk": "slo", "sl": "slv", "so": "som", "sq": "alb", "sr": "srp", "sv": "swe", "sw": "swa",
    "ta": "tam", "te": "tel", "th": "tha", "tl": "tgl", "tr": "tur", "uk": "ukr", "ur": "urd",
    "uz": "uzb", "vi": "vie", "xh": "xho", "yi": "yid", "zh": "chi", "zu": "zul",
}
_LANG_SPLIT = re.compile(r"[-_]")


def _clean(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return None if text.lower() in _INVALID_TEXT else text


def positive_int(value: Any) -> Optional[int]:
    """Positive integer from ints, numeric strings or integral floats; else ``None``."""
    text = _clean(value)
    if text is None:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(number)


def positive_float(value: Any) -> Optional[float]:
    text = _clean(value)
    if text is None:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) and number > 0 else None


def parse_ratio(value: Any) -> Optional[Fraction]:
    """``"16:9"`` / ``"24000/1001"`` / ``"1.78"`` -> positive Fraction, else ``None``."""
    text = _clean(value)
    if text is None:
        return None
    for separator in (":", "/"):
        if separator in text:
            left, _, right = text.partition(separator)
            try:
                numerator, denominator = Fraction(left.strip()), Fraction(right.strip())
            except (ValueError, ZeroDivisionError):
                return None
            if numerator <= 0 or denominator <= 0:
                return None
            return numerator / denominator
    number = positive_float(text)
    return Fraction(number).limit_denominator(100000) if number else None


def tri_state(value: Any) -> Optional[bool]:
    """``True``/``False`` from bools, 0/1 and true/false/yes/no strings; else ``None``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = _clean(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    return None


def normalize_codec(value: Any, codec_tag: Any = None) -> Optional[str]:
    """Lower-case codec name with only well-known aliases applied."""
    tag = (_clean(codec_tag) or "").lower()
    if tag in _XVID_TAGS:
        return "xvid"
    if tag in _DIVX_TAGS:
        return "divx"
    text = _clean(value)
    if text is None:
        return None
    lowered = text.lower()
    return _CODEC_ALIASES.get(lowered, lowered)


def normalize_language(value: Any) -> Optional[str]:
    """Three-letter ISO 639 code. ``en``/``en-US`` -> ``eng``; ``und`` is kept; junk -> ``None``."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().lower()
    if not text or text in ("unknown", "n/a", "none", "null"):
        return None
    primary = _LANG_SPLIT.split(text, 1)[0]
    if len(primary) == 3 and primary.isalpha() and primary.isascii():
        return primary
    if len(primary) == 2:
        return _ISO639_1_TO_2.get(primary)
    return None


def normalize_scan_type(value: Any) -> Optional[str]:
    text = (_clean(value) or "").lower()
    return text if text in SCAN_TYPES else None


def normalize_hdr_type(value: Any) -> Optional[str]:
    text = (_clean(value) or "").lower()
    return text if text in HDR_TYPES else None


def normalize_format_3d(value: Any) -> Optional[str]:
    text = (_clean(value) or "").upper()
    return text if text in FORMAT_3D else None


def normalize_flags(values: Iterable[Any]) -> list[str]:
    """Approved Kodi flags only, deduplicated and sorted for stable output."""
    return sorted({str(v).strip().lower() for v in values if str(v).strip().lower() in APPROVED_FLAGS})


def normalize_aspect_text(value: Any) -> Optional[str]:
    """``"16:9"`` stays ``"16:9"``; invalid ratios (``0:1``, ``N/A``) -> ``None``."""
    text = _clean(value)
    if text is None or ":" not in text or parse_ratio(text) is None:
        return None
    left, _, right = text.partition(":")
    return f"{left.strip()}:{right.strip()}"


def format_decimal(value: float, places: int) -> str:
    """Stable decimal: rounded to ``places``, trailing zeros removed (1.78, 2.4, 25)."""
    text = f"{round(value, places):.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_aspect(value: float) -> str:
    return format_decimal(value, 2)


def format_frame_rate(value: float) -> str:
    return format_decimal(value, 3)
