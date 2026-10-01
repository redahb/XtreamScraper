"""Artwork reconciliation: bring existing artwork in line with the current settings.

Runs as a background job (after an artwork mode change, or on demand). It works from the
persisted selections only – no scraper is contacted – and handles every item that has an
artwork selection: mode migrations (local <-> remote), re-creating missing managed files,
following moved library items, and replacing managed files when asked to.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import Optional

from ..config.settings import Settings
from ..jobs.progress import JobCancelled, JobProgress
from ..library.index import LibraryIndex
from ..library.models import EPISODE, MOVIE
from ..storage.db import Database
from .download import ArtworkDownloader
from .manager import ArtworkManager, ArtworkStats
from .naming import ArtworkTarget

log = logging.getLogger(__name__)


def library_targets(db: Database, provider_id: int) -> dict[tuple, ArtworkTarget]:
    """Current artwork targets of a provider's active library items, by stable item key."""
    index = LibraryIndex(db)
    targets: dict[tuple, ArtworkTarget] = {}
    shows = {(s.category_id, s.series_id): s for s in index.series(provider_id)}
    season_folders: dict[tuple, str] = {}
    for item in index.items(provider_id, kinds=(MOVIE, EPISODE)):
        target = ArtworkTarget.for_movie(item) if item.kind == MOVIE else ArtworkTarget.for_episode(item)
        targets[target.key] = target
        if item.kind == EPISODE:
            season_folders.setdefault((item.category_id, item.series_id, item.season or 0), item.folder)
    for show in shows.values():
        target = ArtworkTarget.for_series(show)
        targets[target.key] = target
    for (category_id, series_id, season), folder in season_folders.items():
        show = shows.get((category_id, series_id))
        if show is not None:
            target = ArtworkTarget.for_season(show, folder, season)
            targets[target.key] = target
    return targets


class ArtworkReconciler:
    def __init__(self, db: Database, settings: Settings, progress: JobProgress, library_root: str,
                 downloader: Optional[ArtworkDownloader] = None) -> None:
        self.db = db
        self.settings = settings
        self.progress = progress
        self.stats = ArtworkStats()
        self.manager = ArtworkManager(db, settings, library_root, downloader=downloader, stats=self.stats)

    def run(self, provider_id: int, label: str = "", force: bool = False) -> tuple[str, ArtworkStats]:
        status = "success"
        log.info("Artwork reconciliation started: %s (mode %s%s)", label, self.settings.artwork_mode,
                 ", force replace" if force else "")
        try:
            self._run(provider_id, force)
        except JobCancelled:
            status = "cancelled"
            self.stats.warn("Artwork reconciliation cancelled by user")
        except Exception as exc:
            status = "failed"
            self.stats.error(f"Unexpected error: {exc}")
            log.exception("Unexpected artwork reconciliation error")
        finally:
            self.manager.close()
        if status == "success" and self.stats.errors_count:
            status = "partial"
        s = self.stats
        log.info("Artwork reconciliation %s: %s – considered %d, downloaded %d, NFO URLs written %d, local removed %d, "
                 "NFO references removed %d, unchanged %d, skipped %d, errors %d", status, label, s.considered,
                 s.downloaded, s.nfo_urls_written, s.local_removed, s.nfo_refs_removed, s.unchanged, s.skipped,
                 s.errors_count)
        return status, self.stats

    def _run(self, provider_id: int, force: bool) -> None:
        targets = library_targets(self.db, provider_id)
        keys = self.manager.repo.item_keys(provider_id)
        by_kind: dict[str, list] = defaultdict(list)
        for key in keys:
            by_kind[key[1]].append(key)
        ordered = [k for kind in ("movie", "series", "season", "episode") for k in sorted(by_kind.get(kind, []))]
        self.progress.update(phase="Artwork", total=len(ordered))
        for key in ordered:
            self.progress.check_cancelled()
            target = targets.get(key)
            if target is None:
                # The item is no longer active in the library: leave its artwork alone.
                self.stats.skipped += 1
                self.progress.advance()
                continue
            self.progress.update(current_item=target.label)
            errors_before = self.stats.errors_count
            try:
                self.manager.process(target, force=force)
            except Exception as exc:
                self.stats.error(f"{target.label}: {exc}")
                log.exception("Artwork reconciliation failed for %s", target.label)
            if self.stats.errors_count > errors_before:
                for message in self.stats.errors[errors_before:][-3:]:
                    self.progress.error(message)
            self.progress.advance()
            self.progress.update(summary=self.stats.summary())


def effective_root(provider_target: str, default_target: str) -> str:
    root = provider_target or default_target
    return os.path.abspath(root) if root else ""
