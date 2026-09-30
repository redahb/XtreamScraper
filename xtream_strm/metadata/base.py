"""The metadata-provider plugin interface.

A provider (TMDB, TVDB, a local scraper...) implements whichever operations it
supports and declares them in :attr:`MetadataProvider.capabilities`. It returns only
normalised models from :mod:`xtream_strm.metadata.models`; it never writes files and
never decides NFO names – :mod:`xtream_strm.nfo` does that.

The synchronization engine does not import this package, so metadata providers stay
optional and replaceable.
"""

from __future__ import annotations

import abc
import enum
from typing import Any, Optional

from .models import (
    Artwork,
    EpisodeMetadata,
    ExternalIds,
    MovieMetadata,
    SearchResult,
    SeasonMetadata,
    TvShowMetadata,
)


class Capability(str, enum.Enum):
    SEARCH_MOVIE = "search_movie"
    SEARCH_TVSHOW = "search_tvshow"
    MOVIE = "movie"
    TVSHOW = "tvshow"
    SEASON = "season"
    EPISODE = "episode"
    ARTWORK = "artwork"
    EXTERNAL_IDS = "external_ids"


class MetadataProviderError(Exception):
    pass


class MetadataProvider(abc.ABC):
    #: Stable identifier, e.g. ``"tmdb"``; also used as ``<uniqueid type>`` / rating name.
    id: str = ""
    #: Human-readable name for the WebUI.
    name: str = ""
    capabilities: frozenset[Capability] = frozenset()

    def configure(self, options: dict[str, Any]) -> None:
        """Receive provider-specific options (API keys, language...)."""

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    # -- search -----------------------------------------------------------------------------
    def search_movie(self, title: str, year: Optional[int] = None) -> list[SearchResult]:
        raise NotImplementedError

    def search_tvshow(self, title: str, year: Optional[int] = None) -> list[SearchResult]:
        raise NotImplementedError

    # -- details ----------------------------------------------------------------------------
    def get_movie(self, ids: ExternalIds) -> Optional[MovieMetadata]:
        raise NotImplementedError

    def get_tvshow(self, ids: ExternalIds) -> Optional[TvShowMetadata]:
        raise NotImplementedError

    def get_season(self, show_ids: ExternalIds, season: int) -> Optional[SeasonMetadata]:
        raise NotImplementedError

    def get_episode(self, show_ids: ExternalIds, season: int, episode: int) -> Optional[EpisodeMetadata]:
        raise NotImplementedError

    def get_artwork(self, ids: ExternalIds, kind: str) -> list[Artwork]:
        raise NotImplementedError

    def get_external_ids(self, ids: ExternalIds, kind: str) -> Optional[ExternalIds]:
        raise NotImplementedError
