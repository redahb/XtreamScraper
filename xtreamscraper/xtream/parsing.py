"""Defensive parsing of Xtream API responses.

Xtream panels are inconsistent: numbers arrive as strings, lists arrive as dicts, a
single object arrives where a list is expected, fields are null or missing entirely.
Every parser here accepts "anything" and returns clean models, skipping individual
malformed records (reported through ``warnings``) instead of failing the whole list.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional, TypeVar

from .models import AccountInfo, Category, Episode, SeasonInfo, SeriesDetails, SeriesEntry, VodStream
from .urls import DEFAULT_EPISODE_EXTENSION, DEFAULT_MOVIE_EXTENSION, clean_extension

T = TypeVar("T")

_YEAR_IN_TEXT = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_SXXEYY = re.compile(r"\bS(\d{1,3})\s*E(\d{1,4})\b", re.IGNORECASE)
_EPISODE_ONLY = re.compile(r"\b(?:E|EP|Episode)\s*(\d{1,4})\b", re.IGNORECASE)


# -- scalar conversion -------------------------------------------------------------------
def to_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                number = float(text)
            except ValueError:
                return None
            return int(number) if number.is_integer() else None
    return None


def to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def to_opt_str(value: Any) -> Optional[str]:
    text = to_str(value)
    return text or None


def to_id(value: Any) -> Optional[str]:
    """Canonical string form of a provider ID (``"0042"`` and ``42`` are the same ID)."""
    number = to_int(value)
    if number is not None:
        return str(number) if number >= 0 else None
    text = to_str(value)
    if text and len(text) <= 64 and not any(c in text for c in "/\\?#"):
        return text
    return None


def valid_year(value: Any) -> Optional[int]:
    year = to_int(value)
    if year is None:
        return None
    return year if 1900 <= year <= datetime.now().year + 5 else None


def year_from_date(value: Any) -> Optional[int]:
    """Year from a provider date like ``2001-10-26`` / ``26/10/2001``; ignores placeholders."""
    text = to_str(value)
    if not text or text.startswith(("1970-01-01", "1900-01-01", "0000")):
        return None
    match = _YEAR_IN_TEXT.search(text)
    return valid_year(match.group(1)) if match else None


def unix_to_iso(value: Any) -> Optional[str]:
    number = to_int(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def as_list(value: Any) -> list[Any]:
    """Lists pass through, dict-of-records becomes its values, a single object is wrapped."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if value and all(isinstance(v, dict) for v in value.values()):
            # {"0": {...}, "1": {...}} style list
            return list(value.values())
        return [value]
    return []


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_all(items: Iterable[Any], parse: Callable[[dict], Optional[T]], label: str, warnings: list[str]) -> list[T]:
    result: list[T] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            warnings.append(f"Skipped malformed {label} record #{index} (not an object)")
            continue
        try:
            parsed = parse(raw)
        except Exception as exc:  # one bad record must never sink the list
            warnings.append(f"Skipped malformed {label} record #{index}: {exc}")
            continue
        if parsed is None:
            name = to_str(raw.get("name") or raw.get("title") or raw.get("category_name"))
            warnings.append(f"Skipped {label} record #{index}{f' ({name})' if name else ''}: missing ID")
            continue
        result.append(parsed)
    return result


# -- categories ---------------------------------------------------------------------------
def _category(raw: dict) -> Optional[Category]:
    cid = to_id(raw.get("category_id"))
    if cid is None:
        return None
    name = to_str(raw.get("category_name")) or f"Category {cid}"
    return Category(category_id=cid, name=name, parent_id=to_id(raw.get("parent_id")))


def parse_categories(data: Any, warnings: Optional[list[str]] = None) -> list[Category]:
    warnings = warnings if warnings is not None else []
    return _parse_all(as_list(data), _category, "category", warnings)


# -- VOD ------------------------------------------------------------------------------------
def _vod(raw: dict) -> Optional[VodStream]:
    sid = to_id(raw.get("stream_id"))
    if sid is None:
        return None
    stream_type = to_str(raw.get("stream_type")).lower()
    if stream_type in ("live", "created_live", "radio_streams"):
        raise ValueError("live stream returned in VOD list")
    release = to_opt_str(raw.get("releaseDate") or raw.get("release_date") or raw.get("releasedate"))
    known = {"stream_id", "name", "container_extension", "category_id", "added", "year", "releaseDate",
             "release_date", "rating", "stream_icon", "tmdb", "tmdb_id", "num", "stream_type"}
    return VodStream(
        stream_id=sid,
        name=to_str(raw.get("name")) or to_str(raw.get("title")),
        extension=clean_extension(raw.get("container_extension"), DEFAULT_MOVIE_EXTENSION),
        category_id=to_id(raw.get("category_id")),
        added=unix_to_iso(raw.get("added")),
        year=valid_year(raw.get("year")),
        release_date=release,
        rating=to_opt_str(raw.get("rating")),
        icon=to_opt_str(raw.get("stream_icon")),
        tmdb_id=to_id(raw.get("tmdb") or raw.get("tmdb_id")) if (raw.get("tmdb") or raw.get("tmdb_id")) else None,
        extra={k: v for k, v in raw.items() if k not in known and isinstance(v, (str, int, float))},
    )


def parse_vod_streams(data: Any, warnings: Optional[list[str]] = None) -> list[VodStream]:
    warnings = warnings if warnings is not None else []
    return _parse_all(as_list(data), _vod, "movie", warnings)


# -- series list -------------------------------------------------------------------------------
def _series(raw: dict) -> Optional[SeriesEntry]:
    sid = to_id(raw.get("series_id"))
    if sid is None:
        return None
    return SeriesEntry(
        series_id=sid,
        name=to_str(raw.get("name")) or to_str(raw.get("title")),
        category_id=to_id(raw.get("category_id")),
        last_modified=to_opt_str(raw.get("last_modified")),
        year=valid_year(raw.get("year")),
        release_date=to_opt_str(raw.get("releaseDate") or raw.get("release_date") or raw.get("releasedate")),
        cover=to_opt_str(raw.get("cover")),
        plot=to_opt_str(raw.get("plot")),
        tmdb_id=to_id(raw.get("tmdb") or raw.get("tmdb_id")) if (raw.get("tmdb") or raw.get("tmdb_id")) else None,
    )


def parse_series_list(data: Any, warnings: Optional[list[str]] = None) -> list[SeriesEntry]:
    warnings = warnings if warnings is not None else []
    return _parse_all(as_list(data), _series, "series", warnings)


# -- series info -------------------------------------------------------------------------------
def episode_numbers_from_title(title: str) -> tuple[Optional[int], Optional[int]]:
    """(season, episode) parsed from titles such as ``Show - S01E05 - Name``."""
    match = _SXXEYY.search(title or "")
    if match:
        return int(match.group(1)), int(match.group(2))
    match = _EPISODE_ONLY.search(title or "")
    if match:
        return None, int(match.group(1))
    return None, None


def _episode(raw: dict, season_hint: Optional[int]) -> Optional[Episode]:
    eid = to_id(raw.get("id") if raw.get("id") is not None else raw.get("episode_id"))
    if eid is None:
        return None
    title = to_str(raw.get("title")) or to_str(raw.get("name"))
    info = as_dict(raw.get("info"))
    season = to_int(raw.get("season"))
    if season_hint is not None:
        # The episodes-dict key is the grouping the panel itself uses; prefer it.
        season = season_hint
    title_season, title_episode = episode_numbers_from_title(title)
    if season is None or season < 0:
        season = title_season if title_season is not None else 1
    number = to_int(raw.get("episode_num"))
    if number is None or number < 0:
        number = title_episode
    duration = to_int(info.get("duration_secs"))
    return Episode(
        episode_id=eid,
        season=season,
        episode_num=number,
        title=title,
        extension=clean_extension(raw.get("container_extension"), DEFAULT_EPISODE_EXTENSION),
        added=unix_to_iso(raw.get("added")),
        plot=to_opt_str(info.get("plot")),
        release_date=to_opt_str(info.get("releasedate") or info.get("release_date") or info.get("air_date")),
        duration_secs=duration if duration and duration > 0 else None,
    )


def _iter_episode_groups(episodes: Any) -> Iterable[tuple[Optional[int], Any]]:
    """Yield (season_hint, raw_episode) for every known episodes layout."""
    if isinstance(episodes, dict):
        for key, value in episodes.items():
            hint = to_int(key)
            if isinstance(value, dict) and "id" not in value and all(isinstance(v, dict) for v in value.values()):
                value = list(value.values())  # {"1": {"0": {...}, "1": {...}}}
            for raw in as_list(value) if not isinstance(value, dict) else [value]:
                yield hint, raw
    elif isinstance(episodes, list):
        for item in episodes:
            if isinstance(item, list):  # [[ep, ep], [ep]] – season grouping without keys
                for raw in item:
                    yield None, raw
            else:
                yield None, item


def parse_series_info(series_id: str, data: Any) -> SeriesDetails:
    details = SeriesDetails(series_id=series_id)
    if not isinstance(data, dict):
        # Some panels answer an empty list for series without episodes.
        details.warnings.append("Series info was not an object; treating as having no episodes")
        return details
    info = as_dict(data.get("info"))
    details.name = to_str(info.get("name"))
    details.plot = to_opt_str(info.get("plot"))
    details.release_date = to_opt_str(info.get("releaseDate") or info.get("release_date") or info.get("releasedate"))

    for raw in as_list(data.get("seasons")):
        if not isinstance(raw, dict):
            continue
        number = to_int(raw.get("season_number"))
        if number is None or number < 0:
            continue
        details.seasons.append(
            SeasonInfo(
                season_number=number,
                name=to_str(raw.get("name")),
                episode_count=to_int(raw.get("episode_count")),
                overview=to_opt_str(raw.get("overview")),
                air_date=to_opt_str(raw.get("air_date")),
                cover=to_opt_str(raw.get("cover_big") or raw.get("cover")),
            )
        )

    seen: set[str] = set()
    for index, (hint, raw) in enumerate(_iter_episode_groups(data.get("episodes"))):
        if not isinstance(raw, dict):
            details.warnings.append(f"Skipped malformed episode record #{index}")
            continue
        try:
            episode = _episode(raw, hint)
        except Exception as exc:
            details.warnings.append(f"Skipped malformed episode record #{index}: {exc}")
            continue
        if episode is None:
            details.warnings.append(f"Skipped episode record #{index}: missing ID")
            continue
        if episode.episode_id in seen:
            continue  # the same episode listed twice
        seen.add(episode.episode_id)
        details.episodes.append(episode)
    return details


# -- account ----------------------------------------------------------------------------------
def parse_account(data: Any) -> AccountInfo:
    if not isinstance(data, dict):
        return AccountInfo(authenticated=False, message="Unexpected response from provider (not a JSON object)")
    user = as_dict(data.get("user_info"))
    if not user:
        return AccountInfo(authenticated=False, message="Provider response has no user_info (wrong URL or credentials?)")
    auth = to_int(user.get("auth"))
    status = to_str(user.get("status"))
    authenticated = auth == 1 or (auth is None and status.lower() == "active")
    return AccountInfo(
        authenticated=authenticated,
        status=status,
        expires_at=unix_to_iso(user.get("exp_date")),
        max_connections=to_int(user.get("max_connections")),
        active_connections=to_int(user.get("active_cons")),
        message=to_str(user.get("message")) or ("" if authenticated else "Provider rejected the credentials"),
    )
