"""Metadata enrichment: fetch normalised metadata from a provider and merge it into NFOs.

Runs against the existing library (see :class:`~xtream_strm.library.index.LibraryIndex`)
without any Xtream synchronization. All file writes go through
:func:`xtream_strm.nfo.service.update_nfo`, so probe-derived ``<fileinfo>`` and any other
fields not supplied by the provider are preserved.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from ..library.models import LibraryItem, LibrarySeries
from ..nfo.metadata_writer import (
    apply_episode_metadata,
    apply_movie_metadata,
    apply_season_metadata,
    apply_tvshow_metadata,
)
from ..nfo.paths import NfoKind, season_nfo_path, tvshow_nfo_path
from ..nfo.service import NfoSeed, NfoUpdateResult, update_nfo
from .base import Capability, MetadataProvider
from .models import EpisodeMetadata, ExternalIds, MovieMetadata, SeasonMetadata, TvShowMetadata

log = logging.getLogger(__name__)


# -- writing already-fetched metadata ------------------------------------------------------------
def write_movie_metadata(item: LibraryItem, meta: MovieMetadata) -> NfoUpdateResult:
    return update_nfo(item.nfo_path, NfoKind.MOVIE, lambda d: apply_movie_metadata(d, meta),
                      NfoSeed(title=item.title, year=item.year))


def write_tvshow_metadata(series: LibrarySeries, meta: TvShowMetadata) -> NfoUpdateResult:
    return update_nfo(tvshow_nfo_path(series.folder_path), NfoKind.TVSHOW, lambda d: apply_tvshow_metadata(d, meta),
                      NfoSeed(title=series.title, year=series.year))


def write_season_metadata(season_folder: str, meta: SeasonMetadata) -> NfoUpdateResult:
    return update_nfo(season_nfo_path(season_folder), NfoKind.SEASON, lambda d: apply_season_metadata(d, meta),
                      NfoSeed(season=meta.season_number))


def write_episode_metadata(item: LibraryItem, meta: EpisodeMetadata) -> NfoUpdateResult:
    return update_nfo(item.nfo_path, NfoKind.EPISODE, lambda d: apply_episode_metadata(d, meta),
                      NfoSeed(season=item.season, episode=item.episode,
                              show_title=item.series_title))


# -- provider-driven enrichment ----------------------------------------------------------------------
class MetadataEnricher:
    def __init__(self, provider: MetadataProvider) -> None:
        self.provider = provider

    def _best(self, results) -> Optional[ExternalIds]:
        if not results:
            return None
        return max(results, key=lambda r: r.score).ids

    def resolve_movie_ids(self, item: LibraryItem) -> Optional[ExternalIds]:
        if not self.provider.supports(Capability.SEARCH_MOVIE):
            return None
        return self._best(self.provider.search_movie(item.title, item.year))

    def resolve_show_ids(self, series: LibrarySeries) -> Optional[ExternalIds]:
        if not self.provider.supports(Capability.SEARCH_TVSHOW):
            return None
        return self._best(self.provider.search_tvshow(series.title, series.year))

    def enrich_movie(self, item: LibraryItem, ids: Optional[ExternalIds] = None) -> Optional[NfoUpdateResult]:
        ids = ids or self.resolve_movie_ids(item)
        if ids is None or not self.provider.supports(Capability.MOVIE):
            return None
        meta = self.provider.get_movie(ids)
        return write_movie_metadata(item, meta) if meta else None

    def enrich_series(self, series: LibrarySeries, episodes: list[LibraryItem],
                      ids: Optional[ExternalIds] = None) -> list[NfoUpdateResult]:
        """TV show NFO, then one season NFO per season folder, then episode NFOs."""
        results: list[NfoUpdateResult] = []
        ids = ids or self.resolve_show_ids(series)
        if ids is None:
            return results
        if self.provider.supports(Capability.TVSHOW):
            show = self.provider.get_tvshow(ids)
            if show:
                results.append(write_tvshow_metadata(series, show))
        if self.provider.supports(Capability.SEASON):
            for season, folder in sorted({(e.season or 0, os.path.dirname(e.strm_path)) for e in episodes}):
                meta = self.provider.get_season(ids, season)
                if meta:
                    results.append(write_season_metadata(folder, meta))
        if self.provider.supports(Capability.EPISODE):
            for episode in episodes:
                meta = self.provider.get_episode(ids, episode.season or 0, episode.episode or 0)
                if meta:
                    results.append(write_episode_metadata(episode, meta))
        return results
