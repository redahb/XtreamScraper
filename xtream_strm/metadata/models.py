"""Normalised metadata models returned by metadata providers.

Providers (TMDB, TVDB, ...) translate their own API responses into these models; the
central NFO subsystem decides how they are written. ``None`` always means "unknown –
leave the NFO field as it is", an empty list means "known to be empty".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExternalIds:
    tmdb: Optional[str] = None
    tvdb: Optional[str] = None
    imdb: Optional[str] = None
    other: dict[str, str] = field(default_factory=dict)

    def items(self) -> list[tuple[str, str]]:
        values = [("tmdb", self.tmdb), ("tvdb", self.tvdb), ("imdb", self.imdb), *self.other.items()]
        return [(k, str(v)) for k, v in values if v not in (None, "")]


@dataclass
class Rating:
    source: str
    value: float
    votes: Optional[int] = None
    max_value: float = 10.0
    default: bool = False


@dataclass
class Person:
    name: str
    role: Optional[str] = None
    thumb: Optional[str] = None
    order: Optional[int] = None


@dataclass
class Artwork:
    url: str
    kind: str = "poster"  # poster | fanart | banner | landscape | clearlogo | clearart | thumb
    season: Optional[int] = None
    preview: Optional[str] = None


@dataclass
class _Common:
    title: Optional[str] = None
    original_title: Optional[str] = None
    plot: Optional[str] = None
    premiered: Optional[str] = None  # YYYY-MM-DD
    year: Optional[int] = None
    genres: Optional[list[str]] = None
    studios: Optional[list[str]] = None
    actors: Optional[list[Person]] = None
    ratings: Optional[list[Rating]] = None
    ids: ExternalIds = field(default_factory=ExternalIds)
    artwork: Optional[list[Artwork]] = None
    mpaa: Optional[str] = None


@dataclass
class MovieMetadata(_Common):
    sort_title: Optional[str] = None
    tagline: Optional[str] = None
    outline: Optional[str] = None
    runtime_minutes: Optional[int] = None
    countries: Optional[list[str]] = None
    directors: Optional[list[str]] = None
    writers: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    collection: Optional[str] = None


@dataclass
class TvShowMetadata(_Common):
    status: Optional[str] = None  # Continuing / Ended


@dataclass
class SeasonMetadata:
    season_number: int
    title: Optional[str] = None
    plot: Optional[str] = None
    premiered: Optional[str] = None
    ids: ExternalIds = field(default_factory=ExternalIds)
    artwork: Optional[list[Artwork]] = None


@dataclass
class EpisodeMetadata:
    season: int
    episode: int
    title: Optional[str] = None
    plot: Optional[str] = None
    aired: Optional[str] = None
    runtime_minutes: Optional[int] = None
    directors: Optional[list[str]] = None
    writers: Optional[list[str]] = None
    actors: Optional[list[Person]] = None
    ratings: Optional[list[Rating]] = None
    ids: ExternalIds = field(default_factory=ExternalIds)
    artwork: Optional[list[Artwork]] = None


@dataclass
class SearchResult:
    provider: str
    kind: str  # "movie" | "tvshow"
    title: str
    year: Optional[int] = None
    ids: ExternalIds = field(default_factory=ExternalIds)
    overview: Optional[str] = None
    score: float = 0.0
