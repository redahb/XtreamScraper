"""Reading Canal+ search records (no I/O).

Every function here is total: odd input gives ``None`` or an empty result, never an exception.
Records are recognised by their ``type`` and their page address (``url``), parsed with
:func:`urllib.parse.urlsplit`, never by substring searches. Object IDs are opaque.
"""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Optional
from urllib.parse import urlsplit

from .sites import NL, Site

log = logging.getLogger(__name__)

MOVIE, SERIES = "movie", "series"
SHOW, SEASON, EPISODE = "show", "showseason", "showepisode"
#: The Dutch website's host (the default site).
SITE_HOST = NL.host
ASSET_HOST = "storage.googleapis.com"
ASSET_BUCKET = "tv-vod-assets"
MIN_YEAR = 1870
MAX_SEASON, MAX_EPISODE = 9999, 99999
#: Opaque Canal+ object IDs: URL-safe and of a sane length (their exact length is not a contract).
OBJECT_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,199}")
_SHORT_EPISODE = re.compile(r"S(\d{1,4})E(\d{1,5})", re.IGNORECASE)
_SHORT_SEASON = re.compile(r"S(\d{1,4})", re.IGNORECASE)
_NUMBER = re.compile(r"(?<!\d)(\d{1,5})(?!\d)")
_YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_ROLE = re.compile(r"(.*?)\s*\(([^()]*)\)")
_ASSET_PART = re.compile(r"[a-z0-9_]{1,40}")
_ASSET_FILE = re.compile(r"([A-Za-z0-9_-]{1,200})_(portrait|landscape)_org\.(jpg|jpeg|png|webp)", re.IGNORECASE)
_TRAILER_SUFFIX = "-trailer"


# -- page addresses -------------------------------------------------------------------------------
class Page(NamedTuple):
    """A Canal+ page address: ``kind`` (``film`` or ``series``), slug and the path below it."""

    kind: str
    slug: str
    rest: tuple[str, ...]


def page_of(url: Any, site: Site = NL) -> Optional[Page]:
    """The parts of an ``https://<site host>/stream/(film|series)/<slug>/...`` address.

    Userinfo, a port, another scheme or host, an empty or odd slug: ``None``. A series address
    may continue with the site's season segment (``seizoen-N/``) and then its episode segment
    (``aflevering-M/``); a film address may not continue. Query and fragment are ignored.
    """
    if not isinstance(url, str) or len(url) > 2000:
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.hostname != site.host or port is not None or "@" in parts.netloc:
        return None
    if not parts.path.startswith("/"):
        return None
    segments = parts.path[1:].split("/")
    if segments[-1] == "":
        segments.pop()  # the usual trailing slash
    if "" in segments:
        return None
    if len(segments) < 3 or segments[0] != "stream" or segments[1] not in ("film", "series"):
        return None
    slug, rest = segments[2], tuple(segments[3:])
    if not _SLUG.fullmatch(slug):
        return None
    if segments[1] == "film" and rest:
        return None
    if rest and not site.season_segment.fullmatch(rest[0]):
        return None
    if len(rest) > 2 or (len(rest) == 2 and not site.episode_segment.fullmatch(rest[1])):
        return None
    return Page(segments[1], slug, rest)


def kind_of(hit: Any, site: Site = NL) -> Optional[str]:
    """``movie`` for a ``show`` record under ``/stream/film/``, ``series`` for any record under
    ``/stream/series/``; ``None`` otherwise, also when a boolean ``tvcpIsMovie`` contradicts the
    address (never a guessed kind)."""
    if not isinstance(hit, dict):
        return None
    page = page_of(hit.get("url"), site)
    if page is None:
        return None
    is_movie = hit.get("tvcpIsMovie")
    if page.kind == "film":
        if hit.get("type") != SHOW or is_movie is False:
            return None
        return MOVIE
    if is_movie is True:
        return None
    return SERIES


def is_trailer_page(hit: Any, site: Site = NL) -> bool:
    """A pseudo-movie whose film address slug ends in ``-trailer`` (e.g. ``snoepjes-trailer``)."""
    page = page_of(hit.get("url"), site) if isinstance(hit, dict) else None
    return page is not None and page.kind == "film" and page.slug.lower().endswith(_TRAILER_SUFFIX)


def series_root(url: Any, site: Site = NL) -> Optional[str]:
    """The canonical ``https://<site host>/stream/series/<slug>/`` of any series address."""
    page = page_of(url, site)
    if page is None or page.kind != "series":
        return None
    return f"https://{site.host}/stream/series/{page.slug}/"


def canonical_path(url: Any, site: Site = NL) -> Optional[str]:
    """``film/<slug>`` or ``series/<slug>`` (a series' season/episode address gives the series)."""
    page = page_of(url, site)
    return None if page is None else f"{page.kind}/{page.slug}"


def in_series(hit: Any, root: str, site: Site = NL) -> bool:
    """``hit``'s address is ``root`` or below it (exact prefix of the canonical address)."""
    url = hit.get("url") if isinstance(hit, dict) else None
    return isinstance(url, str) and series_root(url, site) == root and url.strip().startswith(root)


# -- years and numbers ----------------------------------------------------------------------------
def year_of(text: Any, now_year: Optional[int] = None) -> Optional[int]:
    """The only 4-digit year in ``text`` when plausible (1870 to next year, UTC), else ``None``."""
    if isinstance(text, int) and not isinstance(text, bool):
        text = str(text)
    if not isinstance(text, str) or len(text) > 100:
        return None
    found = _YEAR.findall(text)
    if len(found) != 1:
        return None
    year = int(found[0])
    latest = (now_year if now_year is not None else datetime.datetime.now(datetime.timezone.utc).year) + 1
    return year if MIN_YEAR <= year <= latest else None


def _valid(season: Optional[int], episode: Optional[int]) -> tuple[Optional[int], Optional[int]]:
    if season is None or not 0 <= season <= MAX_SEASON:
        return None, None
    return season, episode if episode is not None and 0 < episode <= MAX_EPISODE else None


def _from_short(text: Any) -> tuple[Optional[int], Optional[int]]:
    """``"S1E1,S01E01"`` -> ``(1, 1)``; ``"S1,S01"`` -> ``(1, None)``. Parts that disagree -> nothing."""
    if not isinstance(text, str) or len(text) > 100:
        return None, None
    found: set[tuple[int, Optional[int]]] = set()
    for part in text.split(","):
        part = part.strip()
        episode = _SHORT_EPISODE.fullmatch(part)
        season = _SHORT_SEASON.fullmatch(part)
        if episode:
            found.add((int(episode.group(1)), int(episode.group(2))))
        elif season:
            found.add((int(season.group(1)), None))
    return _valid(*found.pop()) if len(found) == 1 else (None, None)


def _only_number(text: Any) -> Optional[int]:
    if not isinstance(text, str) or len(text) > 100:
        return None
    found = _NUMBER.findall(text)
    return int(found[0]) if len(found) == 1 else None


def _from_fields(hit: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    """The only number in ``tvcpSeason`` (``"Seizoen 1"``, ``"Staffel 1"``, ``"Évad 1"``...) and in
    ``tvcpEpisode`` (``"Aflevering 3"``...)."""
    return _valid(_only_number(hit.get("tvcpSeason")), _only_number(hit.get("tvcpEpisode")))


def _from_url(hit: dict[str, Any], site: Site) -> tuple[Optional[int], Optional[int]]:
    page = page_of(hit.get("url"), site)
    if page is None or page.kind != "series" or not page.rest:
        return None, None
    season = int(site.season_segment.fullmatch(page.rest[0]).group(1))  # type: ignore[union-attr]
    episode = int(site.episode_segment.fullmatch(page.rest[1]).group(1)) if len(page.rest) == 2 else None  # type: ignore[union-attr]
    return _valid(season, episode)


def season_episode(hit: Any, site: Site = NL) -> tuple[Optional[int], Optional[int]]:
    """``(season, episode)`` of a season/episode record; ``episode`` is ``None`` for a season.

    Sources in order: ``tvcpShort`` (``S1E1,S01E01``), the numbers in ``tvcpSeason`` /
    ``tvcpEpisode`` (any language), the page address (``seizoen-1/aflevering-1/`` on the Dutch site). The first source with both
    numbers wins; otherwise the first with a season. Season 0 (specials) is allowed; an episode is
    ``> 0``; nothing is ever negative.
    """
    if not isinstance(hit, dict):
        return None, None
    sources = [_from_short(hit.get("tvcpShort")), _from_fields(hit), _from_url(hit, site)]
    for season, episode in sources:
        if season is not None and episode is not None:
            return season, episode
    for season, _episode in sources:
        if season is not None:
            return season, None
    return None, None


# -- people ---------------------------------------------------------------------------------------
class Person(NamedTuple):
    name: str
    role: Optional[str] = None  # lower case, e.g. "director" / "producer"


def people(text: Any) -> list[Person]:
    """Comma-separated ``Name`` or ``Name (role)`` entries, de-duplicated case-insensitively
    (first spelling and order kept). A comma inside a name cannot be represented by the source."""
    if not isinstance(text, str):
        return []
    found: list[Person] = []
    seen: set[str] = set()
    for part in text.split(","):
        part = " ".join(part.split())
        match = _ROLE.fullmatch(part)
        name, role = (match.group(1).strip(), match.group(2).strip().lower() or None) if match else (part, None)
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        found.append(Person(name, role))
    return found


def merge_people(*groups: list[Person]) -> list[Person]:
    """The groups concatenated, de-duplicated case-insensitively by name (first one kept)."""
    merged: list[Person] = []
    seen: set[str] = set()
    for group in groups:
        for person in group:
            if person.name.casefold() not in seen:
                seen.add(person.name.casefold())
                merged.append(person)
    return merged


# -- artwork addresses ----------------------------------------------------------------------------
class Asset(NamedTuple):
    """``https://storage.googleapis.com/tv-vod-assets/<provider>/<variant>/<key>_<orientation>_org.<ext>``."""

    provider: str
    variant: str
    key: str
    orientation: str
    extension: str

    @property
    def url(self) -> str:
        return (f"https://{ASSET_HOST}/{ASSET_BUCKET}/{self.provider}/{self.variant}/"
                f"{self.key}_{self.orientation}_org.{self.extension}")


def asset(url: Any) -> Optional[Asset]:
    """The parts of a Canal+ image address, or ``None`` for any other shape (another host or
    scheme, userinfo, a port, a query, an unknown orientation or extension)."""
    if not isinstance(url, str) or len(url) > 1000:
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.hostname != ASSET_HOST or port is not None or "@" in parts.netloc \
            or parts.query or parts.fragment:
        return None
    segments = parts.path.split("/")
    if len(segments) != 5 or segments[0] != "" or segments[1] != ASSET_BUCKET:
        return None
    provider, variant, name = segments[2], segments[3], segments[4]
    match = _ASSET_FILE.fullmatch(name)
    if not _ASSET_PART.fullmatch(provider) or not _ASSET_PART.fullmatch(variant) or not match:
        return None
    return Asset(provider, variant, match.group(1), match.group(2).lower(), match.group(3))


# -- series family --------------------------------------------------------------------------------
@dataclass
class SeriesFamily:
    """The season and episode records of one series.

    ``seasons`` maps every known season number to its ``showseason`` record, or ``None`` when the
    season is only known through its episodes. ``episodes`` keeps every record per ``(season,
    episode)`` so duplicates stay visible. ``complete`` is false when the search answer was
    truncated or inconsistent: an absent season/episode then proves nothing.
    """

    seasons: dict[int, Optional[dict[str, Any]]] = field(default_factory=dict)
    episodes: dict[tuple[int, int], list[dict[str, Any]]] = field(default_factory=dict)
    complete: bool = True

    def season_episodes(self, season: int) -> list[dict[str, Any]]:
        """Every episode record of ``season``, in episode order."""
        return [hit for (s, _e), hits in sorted(self.episodes.items()) if s == season for hit in hits]


def build_family(hits: list[dict[str, Any]], root: str, complete: bool = True, site: Site = NL) -> SeriesFamily:
    """The family of the series at ``root`` from search hits (other series' records are dropped).

    Contract:
        Silences:
            - Records of another type or series are ignored; repeated ``objectID``s are kept once.
            - Season/episode records without usable numbers are skipped (one aggregate log line).
            - A second ``showseason`` record for the same number is ignored (logged).
    """
    family = SeriesFamily(complete=complete)
    seen: set[str] = set()
    skipped = 0
    for hit in hits:
        if not isinstance(hit, dict) or hit.get("type") not in (SEASON, EPISODE) or not in_series(hit, root, site):
            continue
        object_id = hit.get("objectID")
        if not isinstance(object_id, str) or object_id in seen:
            continue
        seen.add(object_id)
        season, episode = season_episode(hit, site)
        if hit["type"] == SEASON:
            if season is None:
                skipped += 1
            elif family.seasons.get(season) is None:
                family.seasons[season] = hit
            else:
                log.info("Canal+ series %s: season %d is listed twice; the first record is used", root, season)
            continue
        if season is None or episode is None:
            skipped += 1
            continue
        family.seasons.setdefault(season, None)
        family.episodes.setdefault((season, episode), []).append(hit)
    if skipped:
        log.info("Canal+ series %s: %d season/episode record(s) without usable numbers skipped", root, skipped)
    return family
