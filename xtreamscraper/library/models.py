"""Library item models shared by synchronization, probing and metadata enrichment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from ..nfo.paths import NfoKind, episode_nfo_path, movie_nfo_path, season_nfo_path, tvshow_nfo_path

MOVIE = "movie"
EPISODE = "episode"


@dataclass(frozen=True)
class LibraryItem:
    """A playable item (movie or episode) with a generated ``.strm`` file."""

    kind: str
    provider_id: int
    category_id: str
    item_id: str
    title: str
    strm_path: str
    status: str = "active"
    year: Optional[int] = None
    series_id: Optional[str] = None
    series_title: Optional[str] = None
    series_folder: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None

    @property
    def folder(self) -> str:
        return os.path.dirname(self.strm_path)

    @property
    def nfo_kind(self) -> NfoKind:
        return NfoKind.MOVIE if self.kind == MOVIE else NfoKind.EPISODE

    @property
    def nfo_path(self) -> str:
        if self.kind == MOVIE:
            return movie_nfo_path(self.folder)
        return episode_nfo_path(self.strm_path)

    @property
    def season_nfo_path(self) -> Optional[str]:
        return season_nfo_path(self.folder) if self.kind == EPISODE else None

    @property
    def label(self) -> str:
        if self.kind == MOVIE:
            return f"{self.title} ({self.year})" if self.year else self.title
        return f"{self.series_title or self.title} S{(self.season or 0):02d}E{(self.episode or 0):02d}"


@dataclass(frozen=True)
class LibrarySeries:
    provider_id: int
    category_id: str
    series_id: str
    title: str
    year: Optional[int]
    folder_path: str
    status: str = "active"

    @property
    def nfo_path(self) -> str:
        return tvshow_nfo_path(self.folder_path)
