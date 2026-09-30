"""Source-independent descriptive metadata model.

Scraper plugins return :class:`MetadataResult`; the core merges results
(:mod:`.merge`) and the NFO subsystem serializes them. Nothing here knows about any
particular metadata provider.

Field semantics: a scalar of ``None`` and a collection of ``None`` both mean "not
supplied". An empty string, empty list, zero rating etc. is also treated as "no
meaningful value" by the merge engine, so a plugin can never erase data with it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class MediaType(str, enum.Enum):
    MOVIE = "movie"
    SERIES = "series"
    SEASON = "season"
    EPISODE = "episode"


class ArtworkType(str, enum.Enum):
    POSTER = "poster"
    FANART = "fanart"  # backdrop
    LOGO = "logo"
    BANNER = "banner"
    LANDSCAPE = "landscape"
    CLEARART = "clearart"
    STILL = "still"  # episode still / thumbnail
    SEASON_POSTER = "season_poster"


@dataclass
class PersonCredit:
    name: str
    role: Optional[str] = None  # character for actors
    order: Optional[int] = None
    external_id: Optional[str] = None  # provider person ID, e.g. "tmdb:17419"
    profile_image: Optional[str] = None
    department: Optional[str] = None
    job: Optional[str] = None


@dataclass
class Artwork:
    type: ArtworkType
    url: str
    language: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    rating: Optional[float] = None
    source: Optional[str] = None
    season_number: Optional[int] = None  # for season posters in tvshow.nfo


@dataclass
class Rating:
    value: float
    votes: Optional[int] = None
    max_value: float = 10.0


@dataclass
class Collection:
    name: str
    overview: Optional[str] = None
    external_id: Optional[str] = None


SCALAR_FIELDS = (
    "title", "original_title", "sort_title", "plot", "outline", "tagline", "year", "premiered",
    "runtime_minutes", "status", "certification", "production_code", "trailer", "collection",
)
LIST_FIELDS = (
    "genres", "countries", "studios", "networks", "tags", "directors", "writers", "actors", "artwork",
)


@dataclass
class MetadataResult:
    media_type: MediaType
    title: Optional[str] = None
    original_title: Optional[str] = None
    sort_title: Optional[str] = None
    plot: Optional[str] = None
    outline: Optional[str] = None
    tagline: Optional[str] = None
    year: Optional[int] = None
    premiered: Optional[str] = None  # YYYY-MM-DD (first aired for series, aired for episodes)
    runtime_minutes: Optional[int] = None
    status: Optional[str] = None
    certification: Optional[str] = None
    production_code: Optional[str] = None
    genres: Optional[list[str]] = None
    countries: Optional[list[str]] = None
    studios: Optional[list[str]] = None
    networks: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    directors: Optional[list[PersonCredit]] = None
    writers: Optional[list[PersonCredit]] = None
    actors: Optional[list[PersonCredit]] = None
    external_ids: dict[str, str] = field(default_factory=dict)  # namespace -> id (tmdb, imdb, tvdb...)
    default_id_type: Optional[str] = None  # which external ID is marked default in the NFO
    ratings: dict[str, Rating] = field(default_factory=dict)  # source -> rating
    default_rating: Optional[str] = None
    collection: Optional[Collection] = None
    artwork: Optional[list[Artwork]] = None
    trailer: Optional[str] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    show_title: Optional[str] = None
    # Provenance of a plugin result (not written to NFO)
    plugin_source: Optional[str] = None
    remote_id: Optional[str] = None
