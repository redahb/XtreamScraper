"""Defensive parsing of Xtream API responses.

Xtream panels are inconsistent: numbers arrive as strings, lists arrive as dicts, a
single object arrives where a list is expected, fields are null or missing entirely.
Every parser here accepts "anything" and returns clean models, skipping individual
malformed records (reported through ``warnings``) instead of failing the whole list.

A skipped record whose stable ID can still be read is reported as "listed but unparsed"
(``ParsedRecords.unparsed_ids`` / ``SeriesDetails.unparsed_episode_ids``): the provider
still lists that item, so the sync must neither update it nor treat it as missing.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator, List, Optional, TypeVar

from .models import AccountInfo, Category, Episode, SeasonInfo, SeriesDetails, SeriesEntry, VodStream
from .urls import DEFAULT_EPISODE_EXTENSION, DEFAULT_MOVIE_EXTENSION, clean_extension

T = TypeVar("T")

# One JSON object from a provider answer (a movie, series, episode, season or category record).
RawRecord = dict[str, Any]

_YEAR_IN_TEXT = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_SXXEYY = re.compile(r"\bS(\d{1,3})\s*E(\d{1,4})\b", re.IGNORECASE)
_EPISODE_ONLY = re.compile(r"\b(?:E|EP|Episode)\s*(\d{1,4})\b", re.IGNORECASE)


# -- scalar conversion -------------------------------------------------------------------
def to_int(value: Any) -> Optional[int]:
    """Integer value of a provider number, or None.

    Contract:
        Silences:
            - Never raises. ``None``, ``bool``, blanks, containers, fractional values
              (``1.5``, ``"1.5"``), ``nan``/``inf`` and unparseable text all give None.
            - Accepts float text: ``"12.0"`` and ``"1e3"`` give 12 and 1000. A digit
              string past Python's int-digit limit falls through to ``float`` and gives
              None (it becomes ``inf``).
    """
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
    """Canonical string form of a provider ID (``"0042"`` and ``42`` are the same ID).

    Contract:
        Silences:
            - Never raises; None means "no usable ID".
            - Anything ``to_int`` reads as an integer becomes its decimal form, so
              ``"1e3"`` -> ``"1000"`` and ``"12.0"`` -> ``"12"``; a negative number gives None.
            - Other text is kept (stripped) when it is 1-64 characters without ``/ \\ ? #``,
              so ``"inf"`` or ``"mv 12"`` are valid text IDs; longer text gives None.
    """
    number = to_int(value)
    if number is not None:
        return str(number) if number >= 0 else None
    text = to_str(value)
    if text and len(text) <= 64 and not any(c in text for c in "/\\?#"):
        return text
    return None


def valid_year(value: Any) -> Optional[int]:
    """``value`` as a year in 1900..(this year + 5), else None.

    Contract:
        Silences:
            - The upper bound reads the local clock (``datetime.now()``), so the same
              input can be rejected today and accepted in a later year.
    """
    year = to_int(value)
    if year is None:
        return None
    return year if 1900 <= year <= datetime.now().year + 5 else None


def year_from_date(value: Any) -> Optional[int]:
    """Year from a provider date like ``2001-10-26`` / ``26/10/2001``; ignores placeholders.

    Contract:
        Silences:
            - Only ISO-ordered placeholders (``1970-01-01``, ``1900-01-01``, ``0000...``)
              are recognised; ``01/01/1970`` yields 1970.
            - Takes the first 19xx/20xx group anywhere in the text, then applies
              ``valid_year`` (clock-dependent upper bound).
    """
    text = to_str(value)
    if not text or text.startswith(("1970-01-01", "1900-01-01", "0000")):
        return None
    match = _YEAR_IN_TEXT.search(text)
    return valid_year(match.group(1)) if match else None


def unix_to_iso(value: Any) -> Optional[str]:
    """UTC ISO-8601 timestamp (seconds) for a Unix time, or None.

    Contract:
        Silences:
            - Zero, negatives, unparseable values and timestamps the platform cannot
              convert (``OverflowError``/``OSError``/``ValueError``) all give None.
    """
    number = to_int(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def as_list(value: Any) -> list[Any]:
    """Lists pass through, dict-of-records becomes its values, a single object is wrapped.

    Contract:
        Silences:
            - Any other dict is wrapped as ``[dict]``, including error objects and ``{}``;
              they then fail as ID-less records. ``is_error_object`` tells such answers apart.
            - Scalars and None give ``[]``. A list is returned as is (not copied).
    """
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


class ParsedRecords(List[T]):
    """Parsed models, plus the stable IDs of listed records that could not be parsed."""

    def __init__(self, items: Iterable[T] = (), unparsed_ids: Iterable[str] = ()) -> None:
        super().__init__(items)
        self.unparsed_ids: set[str] = set(unparsed_ids)


def _raw_id(raw: RawRecord, *keys: str) -> Optional[str]:
    """The record's stable ID read straight from the raw object, if it is usable."""
    for key in keys:
        try:
            value = to_id(raw.get(key))
        except Exception:  # pragma: no cover - to_id is defensive; this is belt and braces
            value = None
        if value is not None:
            return value
    return None


def _parse_all(
    items: Iterable[Any],
    parse: Callable[[RawRecord], Optional[T]],
    label: str,
    warnings: list[str],
    id_key: str,
) -> ParsedRecords[T]:
    """Parse every record, skipping bad ones; one bad record never sinks the list.

    Contract:
        Silences:
            - Non-object records and records ``parse`` returns None for (no ID) are
              skipped with a warning appended to ``warnings``.
            - Any ``Exception`` from ``parse`` is caught: the record is skipped with a
              warning, and if ``id_key`` still yields an ID it goes to ``unparsed_ids``
              (the sync keeps such items as listed, never missing).
            - Duplicates are not removed here.
    """
    result: ParsedRecords[T] = ParsedRecords()
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            warnings.append(f"Skipped malformed {label} record #{index} (not an object)")
            continue
        try:
            parsed = parse(raw)
        except Exception as exc:  # one bad record must never sink the list
            rid = _raw_id(raw, id_key)
            if rid is None:
                warnings.append(f"Skipped malformed {label} record #{index}: {exc}")
            else:
                # Still listed: the sync keeps the stored item as it is (never "missing").
                result.unparsed_ids.add(rid)
                warnings.append(f"Skipped malformed {label} record #{index} (ID {rid}, kept as listed): {exc}")
            continue
        if parsed is None:
            name = to_str(raw.get("name") or raw.get("title") or raw.get("category_name"))
            warnings.append(f"Skipped {label} record #{index}{f' ({name})' if name else ''}: missing ID")
            continue
        result.append(parsed)
    return result


def is_error_object(data: Any, records: ParsedRecords[Any]) -> bool:
    """Did a list endpoint answer an object that holds no record at all?

    Panels answer HTTP 200 with objects such as ``{"user_info": {"auth": 0}}`` (account
    expired or blocked) or ``{"error": "..."}`` (rate limited). Such an answer is a failed
    request, never an empty category: a real empty listing is ``[]``, ``{}`` or an empty
    body, and a real dict-of-records always has at least one readable ID.
    """
    return isinstance(data, dict) and bool(data) and not records and not records.unparsed_ids


# -- categories ---------------------------------------------------------------------------
def _category(raw: RawRecord) -> Optional[Category]:
    cid = to_id(raw.get("category_id"))
    if cid is None:
        return None
    name = to_str(raw.get("category_name")) or f"Category {cid}"
    return Category(category_id=cid, name=name, parent_id=to_id(raw.get("parent_id")))


def parse_categories(data: Any, warnings: Optional[list[str]] = None) -> ParsedRecords[Category]:
    warnings = warnings if warnings is not None else []
    return _parse_all(as_list(data), _category, "category", warnings, "category_id")


def _tmdb_id(raw: RawRecord) -> Optional[str]:
    """The provider's TMDB ID; ``0`` / ``"0"`` (and blanks) are the "none" placeholder."""
    for key in ("tmdb", "tmdb_id"):
        value = to_id(raw.get(key))
        if value is not None and value != "0":
            return value
    return None


# -- VOD ------------------------------------------------------------------------------------
def _vod(raw: RawRecord) -> Optional[VodStream]:
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
        tmdb_id=_tmdb_id(raw),
        extra={k: v for k, v in raw.items() if k not in known and isinstance(v, (str, int, float))},
    )


def parse_vod_streams(data: Any, warnings: Optional[list[str]] = None) -> ParsedRecords[VodStream]:
    warnings = warnings if warnings is not None else []
    return _parse_all(as_list(data), _vod, "movie", warnings, "stream_id")


# -- series list -------------------------------------------------------------------------------
def _series(raw: RawRecord) -> Optional[SeriesEntry]:
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
        tmdb_id=_tmdb_id(raw),
    )


def parse_series_list(data: Any, warnings: Optional[list[str]] = None) -> ParsedRecords[SeriesEntry]:
    warnings = warnings if warnings is not None else []
    return _parse_all(as_list(data), _series, "series", warnings, "series_id")


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


def _episode(raw: RawRecord, season_hint: Optional[int]) -> Optional[Episode]:
    """One episode, or None when it has no usable ID.

    Contract:
        Silences:
            - The ID comes from ``id`` whenever that key is not None, even if it is
              unusable (``""``); only an absent/null ``id`` falls back to ``episode_id``.
            - ``season_hint`` (the episodes-dict key) overrides the record's ``season``.
              A missing or negative season falls back to an ``SxxEyy`` in the title, then 1.
            - A missing/negative ``episode_num`` falls back to the title (may stay None);
              a non-positive duration becomes None.
    """
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


def _iter_episode_groups(episodes: Any) -> Iterator[tuple[Optional[int], Any]]:
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
    """Seasons and episodes from a ``get_series_info`` answer.

    Contract:
        Silences:
            - Never raises for any JSON value. A non-object answer gives no episodes with
              a warning; seasons without a non-negative ``season_number`` are dropped.
            - Episodes: non-objects and ID-less records are skipped with a warning; any
              ``Exception`` from ``_episode`` skips the record, and its ID (``id``, then
              ``episode_id``) goes to ``unparsed_episode_ids`` when readable.
            - An episode ID listed twice keeps its first occurrence only.
    """
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
            rid = _raw_id(raw, "id", "episode_id")
            if rid is None:
                details.warnings.append(f"Skipped malformed episode record #{index}: {exc}")
            else:
                # Still listed: the sync keeps the stored episode as it is (never "missing").
                details.unparsed_episode_ids.add(rid)
                details.warnings.append(f"Skipped malformed episode record #{index} (ID {rid}, kept as listed): {exc}")
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
    """Account state from an authenticate answer.

    Contract:
        Silences:
            - Never raises. A non-object or a missing ``user_info`` gives
              ``authenticated=False`` with an explanatory message. Without an ``auth``
              field, a ``status`` of "active" (any case) counts as authenticated.
    """
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
