"""OMDb response normalization: OMDb JSON -> :class:`~xtreamscraper.metadata.models.MetadataResult`.

OMDb marks unavailable values with ``"N/A"``. Every string goes through :func:`omdb_value`
first, so no ``N/A`` (or blank) ever reaches the normalized model. Only fields the core
model already represents are mapped; OMDb-only fields (Awards, BoxOffice, DVD, Website,
Language, totalSeasons) are ignored rather than turned into new NFO tags.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from xtreamscraper.metadata.models import Artwork, ArtworkType, MediaType, MetadataResult, PersonCredit, Rating

SOURCE = "omdb"
IMDB = "imdb"

_IMDB_ID = re.compile(r"^tt\d{5,}$")
_YEAR = re.compile(r"(?<!\d)(1[89]\d{2}|20\d{2}|2100)(?!\d)")
_RUNTIME = re.compile(r"^(\d+)\s*min\.?$", re.IGNORECASE)
_DATE = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})$")
_MONTHS = {name: number for number, names in enumerate((
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
    ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
    ("dec", "december")), start=1) for name in names}
_NUMBER = r"(\d+(?:\.\d+)?)"
_OUT_OF = re.compile(rf"^{_NUMBER}\s*/\s*{_NUMBER}$")
_PERCENT = re.compile(rf"^{_NUMBER}\s*%$")
_PLAIN = re.compile(rf"^{_NUMBER}$")
_VOTES = re.compile(r"^\d{1,3}(?:[,.\s]\d{3})*$|^\d+$")


def omdb_value(value: Any) -> Optional[str]:
    """The one place ``N/A`` handling lives: ``None``, blanks and ``N/A`` become ``None``."""
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    if not text or text.casefold() == "n/a":
        return None
    return text


def imdb_id(value: Any) -> Optional[str]:
    text = omdb_value(value)
    return text if text and _IMDB_ID.match(text) else None


def parse_year(value: Any) -> Optional[int]:
    """First valid four-digit year (``"2011–2019"`` -> 2011); never a range or a guess."""
    text = omdb_value(value)
    match = _YEAR.search(text) if text else None
    return int(match.group(1)) if match else None


def parse_date(value: Any) -> Optional[str]:
    """``"24 Jun 1994"`` -> ``"1994-06-24"`` (English month names, independent of the locale)."""
    text = omdb_value(value)
    match = _DATE.match(text) if text else None
    if not match:
        return None
    month = _MONTHS.get(match.group(2).casefold())
    if month is None:
        return None
    try:
        return _dt.date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_runtime(value: Any) -> Optional[int]:
    """``"88 min"`` -> 88; anything else (``N/A``, ``"1 h 30"``...) stays unset."""
    text = omdb_value(value)
    match = _RUNTIME.match(text) if text else None
    minutes = int(match.group(1)) if match else 0
    return minutes if minutes > 0 else None


def parse_int(value: Any) -> Optional[int]:
    """Vote counts: ``"1,248,136"`` -> 1248136."""
    text = omdb_value(value)
    if not text or not _VOTES.match(text):
        return None
    number = int(re.sub(r"[,.\s]", "", text))
    return number if number > 0 else None


def split_list(value: Any) -> list[str]:
    """A comma-separated OMDb list, trimmed, without blanks/``N/A``; commas inside () are kept."""
    text = omdb_value(value)
    if not text:
        return []
    parts, depth, current = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [v for v in (omdb_value(p) for p in parts) if v]


def _list(value: Any) -> Optional[list[str]]:
    return split_list(value) or None


# -- people ------------------------------------------------------------------------------------------
_ROLE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")


def _people(value: Any, department: str, job: Optional[str]) -> Optional[list[PersonCredit]]:
    result = []
    for entry in split_list(value):
        name, detail = entry, None
        match = _ROLE.match(entry)
        if match and omdb_value(match.group(1)):
            name, detail = match.group(1).strip(), omdb_value(match.group(2))
        result.append(PersonCredit(name=name, department=department, job=detail or job))
    return result or None


def _actors(value: Any) -> Optional[list[PersonCredit]]:
    # OMDb lists principal actors only, without characters: no role is guessed.
    return [PersonCredit(name=name, order=index, department="Acting")
            for index, name in enumerate(split_list(value))] or None


# -- ratings ------------------------------------------------------------------------------------------
def _score(value: Any, pattern: re.Pattern, maximum: float) -> Optional[float]:
    text = omdb_value(value)
    match = pattern.match(text) if text else None
    if not match:
        return None
    number = float(match.group(1))
    if pattern is _OUT_OF:
        if float(match.group(2)) != maximum:
            return None
    return number if 0 < number <= maximum else None


def parse_ratings(data: dict[str, Any]) -> dict[str, Rating]:
    """Source-keyed ratings: ``imdb`` (0–10 with votes), ``rottentomatoes`` and ``metacritic`` (0–100).

    The dedicated fields and the ``Ratings`` list describe the same ratings; each source
    appears at most once. Unknown sources are ignored.
    """
    listed: dict[str, Any] = {}
    for entry in data.get("Ratings") or []:
        if isinstance(entry, dict):
            source = (omdb_value(entry.get("Source")) or "").casefold()
            if source and source not in listed:
                listed[source] = entry.get("Value")
    ratings: dict[str, Rating] = {}
    imdb = _score(data.get("imdbRating"), _PLAIN, 10.0)
    if imdb is None:
        imdb = _score(listed.get("internet movie database"), _OUT_OF, 10.0)
    if imdb is not None:
        ratings["imdb"] = Rating(imdb, parse_int(data.get("imdbVotes")), 10.0)
    tomatoes = _score(listed.get("rotten tomatoes"), _PERCENT, 100.0)
    if tomatoes is not None:
        ratings["rottentomatoes"] = Rating(tomatoes, None, 100.0)
    metacritic = _score(listed.get("metacritic"), _OUT_OF, 100.0)
    if metacritic is None:
        metacritic = _score(data.get("Metascore"), _PLAIN, 100.0)
    if metacritic is not None:
        ratings["metacritic"] = Rating(metacritic, None, 100.0)
    return ratings


# -- mapping ------------------------------------------------------------------------------------------
def media_type_of(data: dict[str, Any]) -> Optional[str]:
    return (omdb_value(data.get("Type")) or "").casefold() or None


def map_details(data: dict[str, Any], media: MediaType, poster_ref: Optional[str] = None) -> MetadataResult:
    """A full (``plot=full``) OMDb movie, series or episode record.

    ``poster_ref`` (movies/series only) marks the public poster as also available through the
    plugin's authenticated Poster API for local downloads.
    """
    remote = imdb_id(data.get("imdbID"))
    ratings = parse_ratings(data)
    artwork = None
    poster = omdb_value(data.get("Poster"))
    if poster and poster.lower().startswith(("http://", "https://")):
        kind = ArtworkType.EPISODE_STILL if media is MediaType.EPISODE else ArtworkType.POSTER
        artwork = [Artwork(kind, poster, source=SOURCE,
                           download_ref=poster_ref if media is not MediaType.EPISODE else None)]
    return MetadataResult(
        media_type=media,
        title=omdb_value(data.get("Title")),
        year=parse_year(data.get("Year")),
        certification=omdb_value(data.get("Rated")),
        premiered=parse_date(data.get("Released")),
        runtime_minutes=parse_runtime(data.get("Runtime")),
        genres=_list(data.get("Genre")),
        directors=_people(data.get("Director"), "Directing", "Director"),
        writers=_people(data.get("Writer"), "Writing", "Writer"),
        actors=_actors(data.get("Actors")),
        plot=omdb_value(data.get("Plot")),
        countries=_list(data.get("Country")),
        studios=_list(data.get("Production")),
        external_ids={IMDB: remote} if remote else {},
        default_id_type=IMDB if remote else None,
        ratings=ratings,
        default_rating="imdb" if "imdb" in ratings else None,
        artwork=artwork,
        plugin_source=SOURCE,
        remote_id=remote,
    )


@dataclass
class SeasonEpisode:
    number: int
    title: Optional[str] = None
    released: Optional[str] = None
    imdb_id: Optional[str] = None
    imdb_rating: Optional[float] = None


@dataclass
class SeasonListing:
    """``i=<series>&Season=N``. Informational only: OMDb listings can be incomplete, so a
    missing entry never means a local episode does not exist."""

    title: Optional[str] = None
    season: Optional[int] = None
    total_seasons: Optional[int] = None
    episodes: list[SeasonEpisode] = field(default_factory=list)

    def episode(self, number: int) -> Optional[SeasonEpisode]:
        return next((e for e in self.episodes if e.number == number), None)


def positive_int(value: Any) -> Optional[int]:
    text = omdb_value(value)
    return int(text) if text and text.isdigit() and int(text) > 0 else None


def parse_season_listing(data: dict[str, Any]) -> SeasonListing:
    episodes = []
    for entry in data.get("Episodes") or []:
        if not isinstance(entry, dict):
            continue
        number = positive_int(entry.get("Episode"))
        if number is None:
            continue
        episodes.append(SeasonEpisode(number, omdb_value(entry.get("Title")), parse_date(entry.get("Released")),
                                      imdb_id(entry.get("imdbID")), _score(entry.get("imdbRating"), _PLAIN, 10.0)))
    episodes.sort(key=lambda e: e.number)
    season = omdb_value(data.get("Season"))
    return SeasonListing(omdb_value(data.get("Title")), int(season) if season and season.isdigit() else None,
                         positive_int(data.get("totalSeasons")), episodes)
