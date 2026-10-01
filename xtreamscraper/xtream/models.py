"""Normalised Xtream data models (VOD and Series only; Live TV is deliberately absent)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Category:
    category_id: str
    name: str
    parent_id: Optional[str] = None


@dataclass
class VodStream:
    stream_id: str
    name: str
    extension: str
    category_id: Optional[str] = None
    added: Optional[str] = None
    year: Optional[int] = None  # only when the provider states it explicitly
    release_date: Optional[str] = None
    rating: Optional[str] = None
    icon: Optional[str] = None
    tmdb_id: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SeriesEntry:
    series_id: str
    name: str
    category_id: Optional[str] = None
    last_modified: Optional[str] = None
    year: Optional[int] = None
    release_date: Optional[str] = None
    cover: Optional[str] = None
    plot: Optional[str] = None
    tmdb_id: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Episode:
    episode_id: str
    season: int
    episode_num: Optional[int]
    title: str
    extension: str
    added: Optional[str] = None
    plot: Optional[str] = None
    release_date: Optional[str] = None
    duration_secs: Optional[int] = None


@dataclass
class SeasonInfo:
    season_number: int
    name: str = ""
    episode_count: Optional[int] = None
    overview: Optional[str] = None
    air_date: Optional[str] = None
    cover: Optional[str] = None


@dataclass
class SeriesDetails:
    series_id: str
    name: str = ""
    plot: Optional[str] = None
    release_date: Optional[str] = None
    seasons: list[SeasonInfo] = field(default_factory=list)
    episodes: list[Episode] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class AccountInfo:
    authenticated: bool
    status: str = ""
    expires_at: Optional[str] = None
    max_connections: Optional[int] = None
    active_connections: Optional[int] = None
    message: str = ""

    def summary(self) -> str:
        if not self.authenticated:
            return self.message or "Authentication failed"
        parts = [f"Authenticated (status: {self.status or 'unknown'})"]
        if self.expires_at:
            parts.append(f"expires {self.expires_at}")
        if self.max_connections is not None:
            parts.append(f"max connections {self.max_connections}")
        return ", ".join(parts)
