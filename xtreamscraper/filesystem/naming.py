"""Human-readable names for movies, series, seasons and episodes.

Title clean-up follows the reference project's behaviour: provider decorations such as
language tags (``|EN|``), quality/codec/source tags (``4K``, ``HEVC``, ``WEB-DL``) and a
trailing year are removed from the display title; the year is added back in the
canonical ``Title (Year)`` form when it is reliably known.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..xtream.parsing import valid_year, year_from_date
from .sanitize import DEFAULT_MAX_LENGTH, UNKNOWN, sanitize_component

_YEAR_PARENS = re.compile(r"\s*[\(\[]((?:19|20)\d{2})[\)\]]\s*$")
_YEAR_DASH = re.compile(r"\s*[-–]\s*((?:19|20)\d{2})\s*$")
_PREFIX_LANG = re.compile(r"^\s*[|┃]\s*[A-Z]{2,3}\s*[|┃]\s*", re.IGNORECASE)
_LANG_TAG = re.compile(r"[|┃\[]\s*[A-Z]{2,3}\s*[|┃\]]", re.IGNORECASE)
_LANG_PHRASE = re.compile(
    r"[\(\[]\s*(?:EN|UK|DE|FR|ES|IT|NL|PT|RU|PL|JP|KR|CN|TR|AR)\s*"
    r"(?:SPOKEN|DUBBED|SUBS?|SUBBED|OV|OmU|AUDIO|MULTI)?-?\s*[\)\]]",
    re.IGNORECASE,
)
_CJK_BRACKETED = re.compile(r"\s*[\[\(][^\]\)]*[　-鿿가-힯぀-ヿ]+[^\]\)]*[\]\)]")
_CODEC = re.compile(r"\b(?:HEVC|[xh]\.?26[45]|AVC|VP9|AV1|10-?bit|8-?bit)\b", re.IGNORECASE)
_QUALITY = re.compile(r"\b(?:4K|UHD|2160p|1080[pi]|720p|480p|576p|HDR10\+?|HDR|SDR|FHD)\b", re.IGNORECASE)
_SOURCE = re.compile(r"\b(?:Blu-?Ray|BRRip|BDRip|WEB-?(?:Rip|DL)|HDTV|DVDRip|REMUX)\b", re.IGNORECASE)
_EMPTY_BRACKETS = re.compile(r"\[\s*\]|\(\s*\)")
_MALFORMED_QUOTES = re.compile(r"'\\''|'\\'|\\''")
_SPACES = re.compile(r"\s{2,}")
_DANGLING = re.compile(r"(?:\s*[-–|]\s*)+$")


def extract_year(name: Optional[str]) -> Optional[int]:
    """Year stated at the end of a title: ``Movie (1999)``, ``Movie [1999]`` or ``Movie - 1999``."""
    if not name:
        return None
    stripped = _strip_tags(name)
    for pattern in (_YEAR_PARENS, _YEAR_DASH):
        match = pattern.search(stripped)
        if match:
            return valid_year(match.group(1))
    return None


def _strip_tags(name: str) -> str:
    text = _PREFIX_LANG.sub("", name)
    text = _LANG_TAG.sub("", text)
    text = _LANG_PHRASE.sub("", text)
    text = _CJK_BRACKETED.sub("", text)
    text = _CODEC.sub("", text)
    text = _QUALITY.sub("", text)
    text = _SOURCE.sub("", text)
    text = _EMPTY_BRACKETS.sub("", text)
    return _SPACES.sub(" ", text).strip()


def version_label(name: Optional[str]) -> Optional[str]:
    """Quality/codec/source tags in a title (``4K HEVC``) – used to tell variants apart."""
    if not name:
        return None
    labels: list[str] = []
    for pattern in (_CODEC, _QUALITY, _SOURCE):
        labels.extend(m.group(0).upper() for m in pattern.finditer(name))
    seen: list[str] = []
    for label in labels:
        if label not in seen:
            seen.append(label)
    return " ".join(seen) or None


def clean_title(name: Optional[str], enabled: bool = True) -> str:
    """Display title without provider decorations and without a trailing year."""
    text = (name or "").strip()
    if not text:
        return ""
    if enabled:
        stripped = _strip_tags(text)
        text = stripped or text
    text = _MALFORMED_QUOTES.sub("'", text)
    for pattern in (_YEAR_PARENS, _YEAR_DASH):
        without = pattern.sub("", text)
        if without.strip():
            text = without
    text = _DANGLING.sub("", text)
    return _SPACES.sub(" ", text).strip()


def resolve_year(name: Optional[str], explicit_year: object = None, release_date: object = None) -> Optional[int]:
    """Year from the title, else an explicit provider year, else a release date. Never guessed."""
    return extract_year(name) or valid_year(explicit_year) or year_from_date(release_date)


@dataclass(frozen=True)
class NameRules:
    max_length: int = DEFAULT_MAX_LENGTH
    clean_titles: bool = True
    episode_title_in_filename: bool = False


def title_with_year(title: str, year: Optional[int]) -> str:
    return f"{title} ({year})" if year else title


def movie_base_name(raw_name: str, year: Optional[int], rules: NameRules = NameRules()) -> str:
    title = clean_title(raw_name, rules.clean_titles) or UNKNOWN
    # Leave room for " (YYYY)" and a possible collision suffix.
    safe_title = sanitize_component(title, max_length=max(20, rules.max_length - 20))
    return sanitize_component(title_with_year(safe_title, year), max_length=rules.max_length)


def series_display_title(raw_name: str, rules: NameRules = NameRules()) -> str:
    title = clean_title(raw_name, rules.clean_titles) or UNKNOWN
    return sanitize_component(title, max_length=max(20, rules.max_length - 40))


def series_base_name(raw_name: str, year: Optional[int], rules: NameRules = NameRules()) -> str:
    return sanitize_component(title_with_year(series_display_title(raw_name, rules), year), max_length=rules.max_length)


def season_folder_name(season: int) -> str:
    """``Season 00`` (Specials), ``Season 01`` ... ``Season 100``."""
    return f"Season {max(0, int(season)):02d}"


def episode_code(season: int, episode: Optional[int]) -> str:
    return f"S{max(0, season):02d}E{max(0, episode or 0):02d}"


def episode_base_name(
    series_title: str,
    season: int,
    episode: Optional[int],
    episode_title: str = "",
    rules: NameRules = NameRules(),
) -> str:
    """``Breaking Bad S01E01`` (optionally ``Breaking Bad S01E01 - Pilot``)."""
    base = f"{series_title} {episode_code(season, episode)}"
    if rules.episode_title_in_filename:
        title = trustworthy_episode_title(series_title, season, episode, episode_title)
        if title:
            base = f"{base} - {title}"
    return sanitize_component(base, max_length=rules.max_length)


def trustworthy_episode_title(series_title: str, season: int, episode: Optional[int], title: str) -> str:
    """The episode's own title, or '' if it is only a restatement of show/number."""
    text = clean_title(title, True)
    if not text:
        return ""
    code = episode_code(season, episode)
    # "Show - S01E02 - Title" -> "Title"
    index = text.upper().find(code)
    if index >= 0:
        text = text[index + len(code):].strip(" -–:")
    if text.lower().startswith(series_title.lower()):
        text = text[len(series_title):].strip(" -–:")
    if not text or re.fullmatch(r"(?:episode|ep|e)?\s*\d+", text, re.IGNORECASE):
        return ""
    return sanitize_component(text, max_length=60, fallback="")
