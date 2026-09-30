"""Metadata scrape jobs.

For every library item::

    existing NFO -> MetadataResult -> plugin 1 (merge) -> plugin 2 (merge) ... -> NFO written once

Matching order, per plugin (the same for every plugin, owned by the core):

1. a stored valid binding (remote ID) is fetched directly; a definitive "not found"
   invalidates it and falls through, an API error does NOT trigger a rematch;
2. an ID in the plugin's namespace already present in the NFO (``<uniqueid type=...>``);
3. the plugin's own matching (external-ID lookups, then title/year search).

Series are matched once; seasons and episodes are fetched with the series' remote ID
plus the local season/episode numbers, never matched separately. Scraping never renames
files and never touches ``<fileinfo>``.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any, Callable, Optional

from ..config.settings import Settings
from ..filesystem import atomic
from ..jobs.progress import JobCancelled, JobProgress
from ..library.index import LibraryIndex
from ..library.models import EPISODE, MOVIE, LibraryItem, LibrarySeries
from ..nfo.document import NfoDocument, NfoError
from ..nfo.metadata_parser import parse_metadata
from ..nfo.metadata_writer import apply_metadata
from ..nfo.paths import NfoKind, season_nfo_path, tvshow_nfo_path
from ..nfo.service import update_nfo
from ..storage.db import Database
from ..storage.scrapers import (
    AMBIGUOUS,
    API_ERROR,
    INVALID_BINDING,
    MATCHED,
    NOT_FOUND,
    UNMATCHED,
    Binding,
    ScraperStateRepository,
)
from ..utils.redact import redact
from ..utils.timeutil import now_iso, older_than
from .manager import ActivePlugin, ScraperManager
from .merge import descriptive_equal, meaningful, merge_into, snapshot
from .models import MediaType, MetadataResult
from .plugin import Capability, ScraperSession
from .results import FetchOutcome, FetchStatus, MatchMethod, MatchQuery, MatchStatus

log = logging.getLogger(__name__)
MAX_MESSAGES = 500
UNSUPPORTED = "unsupported"


@dataclass
class ScrapeRequest:
    provider_id: Optional[int] = None
    kinds: tuple[str, ...] = (MOVIE, "series")
    force: bool = False

    @property
    def scope(self) -> str:
        if MOVIE in self.kinds and "series" in self.kinds:
            return "all"
        return "movies" if MOVIE in self.kinds else "series"


@dataclass
class ScrapeStats:
    considered: int = 0
    matched: int = 0
    unmatched: int = 0
    ambiguous: int = 0
    not_found: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    errors_count: int = 0
    plugins_used: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        if len(self.warnings) < MAX_MESSAGES:
            self.warnings.append(redact(message))

    def error(self, message: str) -> None:
        self.errors_count += 1
        if len(self.errors) < MAX_MESSAGES:
            self.errors.append(redact(message))

    def counters(self) -> dict[str, int]:
        return {k: v for k, v in asdict(self).items() if isinstance(v, int)}

    def summary(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in
                ("considered", "matched", "unmatched", "ambiguous", "updated", "unchanged", "skipped")}


@dataclass
class PluginOutcome:
    plugin_id: str
    status: str
    metadata: Optional[MetadataResult] = None
    binding: Optional[Binding] = None
    message: Optional[str] = None


class ScrapeEngine:
    def __init__(self, db: Database, settings: Settings, manager: ScraperManager, progress: JobProgress) -> None:
        self.db = db
        self.settings = settings
        self.manager = manager
        self.progress = progress
        self.state = ScraperStateRepository(db)
        self.index = LibraryIndex(db)
        self.stats = ScrapeStats()
        self.active: list[ActivePlugin] = []
        self.sessions: dict[str, ScraperSession] = {}
        self.retry_after = timedelta(days=max(0, settings.metadata_retry_unmatched_days))
        self.force = False

    # -- entry point -------------------------------------------------------------------------------
    def run(self, request: ScrapeRequest, label: str = "") -> tuple[str, ScrapeStats]:
        status = "success"
        self.force = request.force
        log.info("Metadata scrape started: %s (%s%s)", label or "all providers", request.scope,
                 ", forced" if request.force else "")
        try:
            self._run(request)
        except JobCancelled:
            status = "cancelled"
            self.stats.warn("Metadata scrape cancelled by user")
        except Exception as exc:
            status = "failed"
            self.stats.error(f"Unexpected error: {exc}")
            log.exception("Unexpected metadata scrape error")
        finally:
            for session in self.sessions.values():
                try:
                    session.close()
                except Exception:
                    log.exception("Closing scraper session failed")
        if status == "success" and self.stats.errors_count:
            status = "partial"
        if status == "success" and not self.active:
            status = "failed"
        s = self.stats
        log.info("Metadata scrape %s: %s – considered %d, matched %d, unmatched %d, ambiguous %d, "
                 "updated %d, unchanged %d, skipped %d, errors %d", status, label or "all providers",
                 s.considered, s.matched, s.unmatched, s.ambiguous, s.updated, s.unchanged, s.skipped, s.errors_count)
        return status, self.stats

    def _run(self, request: ScrapeRequest) -> None:
        self.active, warnings = self.manager.active_plugins()
        for warning in warnings:
            self.stats.warn(warning)
        if not self.active:
            self.stats.error("No enabled and configured metadata scraper plugins")
            return
        for ap in self.active:
            try:
                self.sessions[ap.plugin.plugin_id] = ap.plugin.create_session(ap.config)
                self.stats.plugins_used.append(ap.plugin.plugin_id)
            except Exception as exc:
                self.stats.error(f"{ap.plugin.plugin_id}: could not start: {exc}")
        self.active = [ap for ap in self.active if ap.plugin.plugin_id in self.sessions]

        movies = list(self.index.items(request.provider_id, kinds=(MOVIE,))) if MOVIE in request.kinds else []
        series = list(self.index.series(request.provider_id)) if "series" in request.kinds else []
        episodes: dict[tuple, list[LibraryItem]] = defaultdict(list)
        if series:
            for ep in self.index.items(request.provider_id, kinds=(EPISODE,)):
                episodes[(ep.provider_id, ep.category_id, ep.series_id)].append(ep)
        seasons_total = sum(len({e.season or 0 for e in eps}) for eps in episodes.values())
        self.progress.update(phase="Scraping", total=len(movies) + len(series) + seasons_total
                             + sum(len(v) for v in episodes.values()))

        for movie in movies:
            self.progress.check_cancelled()
            self._guard(movie.label, lambda m=movie: self._movie(m))
            self.progress.advance()
        for show in series:
            self.progress.check_cancelled()
            eps = episodes.get((show.provider_id, show.category_id, show.series_id), [])
            self._guard(show.title, lambda s=show, e=eps: self._series(s, e))

    def _guard(self, label: str, action: Callable[[], None]) -> None:
        """One failing item never stops the job."""
        try:
            self.progress.update(current_item=label)
            action()
        except JobCancelled:
            raise
        except Exception as exc:
            self.stats.error(f"{label}: {exc}")
            self.progress.error(f"{label}: {exc}")
            log.exception("Metadata scrape failed for %s", label)
        self.progress.update(summary=self.stats.summary())

    # -- incremental skip ----------------------------------------------------------------------------
    def _needs(self, ap: ActivePlugin, key: tuple, nfo_exists: bool) -> bool:
        if self.force:
            return True
        binding = self.state.get_binding(ap.plugin.plugin_id, *key)
        if binding is None or binding.config_fingerprint != ap.config_fingerprint:
            return True
        if binding.status == MATCHED:
            return not (binding.last_successful_scrape and nfo_exists)
        if binding.status in (UNMATCHED, AMBIGUOUS, NOT_FOUND, INVALID_BINDING):
            return older_than(binding.last_attempt, self.retry_after)
        return True  # api_error or unknown

    def _item_needs(self, key: tuple, nfo_path: str) -> bool:
        exists = atomic.is_file(nfo_path)
        return any(self._needs(ap, key, exists) for ap in self.active)

    # -- the pipeline ---------------------------------------------------------------------------------
    def _pipeline(self, label: str, nfo_path: str, kind: NfoKind, outcomes_for: Callable[[MetadataResult], list[PluginOutcome]],
                  local_fill: Callable[[MetadataResult], None]) -> list[PluginOutcome]:
        exists = atomic.is_file(nfo_path)
        try:
            existing = parse_metadata(NfoDocument.load(nfo_path, kind), kind) if exists \
                else MetadataResult(media_type=_media_type(kind))
        except NfoError as exc:
            self.stats.error(f"{label}: existing NFO cannot be read, left untouched: {exc}")
            return []
        result = snapshot(existing)
        outcomes = outcomes_for(existing)
        for ap in self.active:  # merge strictly in priority order
            outcome = next((o for o in outcomes if o.plugin_id == ap.plugin.plugin_id), None)
            if outcome is None or outcome.status != MATCHED or outcome.metadata is None:
                continue
            meta = outcome.metadata
            namespace = ap.plugin.id_namespace
            if namespace and outcome.binding and outcome.binding.remote_id and namespace not in meta.external_ids \
                    and kind in (NfoKind.MOVIE, NfoKind.TVSHOW):
                meta.external_ids[namespace] = outcome.binding.remote_id
            merge_into(result, meta, overwrite=ap.state.overwrite)
        local_fill(result)
        self._count(label, outcomes)
        if descriptive_equal(result, existing) or (not exists and not any(o.status == MATCHED for o in outcomes)):
            self.stats.unchanged += 1
            return outcomes
        try:
            update_nfo(nfo_path, kind, lambda doc: apply_metadata(doc, result))
            self.stats.updated += 1
        except (NfoError, OSError) as exc:
            self.stats.error(f"{label}: NFO write failed: {exc}")
        return outcomes

    def _count(self, label: str, outcomes: list[PluginOutcome]) -> None:
        self.stats.considered += 1
        statuses = {o.status for o in outcomes}
        for o in outcomes:
            if o.status == API_ERROR:
                self.stats.error(f"{label} [{o.plugin_id}]: {o.message or 'request failed'}")
        if MATCHED in statuses:
            self.stats.matched += 1
        elif AMBIGUOUS in statuses:
            self.stats.ambiguous += 1
        elif NOT_FOUND in statuses:
            self.stats.not_found += 1
        elif statuses & {UNMATCHED, INVALID_BINDING}:
            self.stats.unmatched += 1

    # -- bindings --------------------------------------------------------------------------------------
    def _binding(self, ap: ActivePlugin, key: tuple, old: Optional[Binding], **values: Any) -> Binding:
        base = asdict(old) if old else {"plugin_id": ap.plugin.plugin_id, "item_kind": key[0],
                                          "provider_id": key[1], "category_id": key[2], "item_id": key[3],
                                          "status": UNMATCHED}
        base.update(values, last_attempt=now_iso(), config_fingerprint=ap.config_fingerprint)
        binding = Binding(**base)
        self.state.save_binding(binding)
        return binding

    def _call(self, fn: Callable[[], Any], plugin_id: str):
        """Plugin calls are isolated: an exception becomes an API error outcome."""
        try:
            return fn()
        except Exception as exc:
            log.warning("Scraper %s raised: %s", plugin_id, redact(exc))
            return FetchOutcome.error(f"plugin error: {exc}")

    def _resolve_root(self, ap: ActivePlugin, key: tuple, media: str, title: str, year: Optional[int],
                      existing: MetadataResult) -> PluginOutcome:
        pid = ap.plugin.plugin_id
        session = self.sessions[pid]
        fetch = session.get_movie if media == "movie" else session.get_series
        match = session.match_movie if media == "movie" else session.match_series
        old = self.state.get_binding(pid, *key)
        invalid_id: Optional[str] = None
        now = now_iso()

        def success(remote_id: str, outcome: FetchOutcome, method: str, score=None, mtitle=None, myear=None) -> PluginOutcome:
            binding = self._binding(ap, key, old, status=MATCHED, remote_id=remote_id, match_method=method,
                                    match_score=score, matched_title=mtitle or (outcome.metadata.title if outcome.metadata else None),
                                    matched_year=myear or (outcome.metadata.year if outcome.metadata else None),
                                    matched_at=now if not (old and old.remote_id == remote_id and old.matched_at) else old.matched_at,
                                    last_successful_scrape=now, message=None, candidates=[])
            return PluginOutcome(pid, MATCHED, outcome.metadata, binding)

        def failed(status: str, message: Optional[str], **extra) -> PluginOutcome:
            binding = self._binding(ap, key, old, status=status, message=message, **extra)
            return PluginOutcome(pid, status, None, binding, message)

        # 1. stored binding
        if old and old.remote_id and old.status in (MATCHED, API_ERROR):
            outcome = self._call(lambda: fetch(old.remote_id), pid)
            if outcome.status is FetchStatus.OK:
                return success(old.remote_id, outcome, old.match_method or MatchMethod.STORED_BINDING.value,
                               old.match_score, old.matched_title, old.matched_year)
            if outcome.status is FetchStatus.UNSUPPORTED:
                return PluginOutcome(pid, UNSUPPORTED)
            if outcome.status is FetchStatus.API_ERROR:
                return failed(API_ERROR, outcome.message)  # keep remote_id; never rematch on errors
            invalid_id = old.remote_id
            log.info("%s: stored %s binding %s no longer exists; matching again", title, pid, invalid_id)

        # 2. ID already present in the NFO for this plugin's namespace
        namespace = ap.plugin.id_namespace
        nfo_id = existing.external_ids.get(namespace) if namespace else None
        if nfo_id and nfo_id != invalid_id:
            outcome = self._call(lambda: fetch(nfo_id), pid)
            if outcome.status is FetchStatus.OK:
                return success(nfo_id, outcome, MatchMethod.NFO_UNIQUEID.value)
            if outcome.status is FetchStatus.API_ERROR:
                return failed(API_ERROR, outcome.message, remote_id=None)
            if outcome.status is FetchStatus.UNSUPPORTED:
                return PluginOutcome(pid, UNSUPPORTED)

        # 3./4. plugin matching (external IDs, then title/year)
        query = MatchQuery(media_type=media, title=title, year=year,
                           external_ids={k: v for k, v in existing.external_ids.items() if k != namespace or v != invalid_id})
        result = self._call(lambda: match(query), pid)
        status = getattr(result, "status", None)
        if status is MatchStatus.UNSUPPORTED or status is FetchStatus.UNSUPPORTED:
            return PluginOutcome(pid, UNSUPPORTED)
        if status is MatchStatus.MATCHED and result.remote_id and result.remote_id != invalid_id:
            outcome = self._call(lambda: fetch(result.remote_id), pid)
            if outcome.status is FetchStatus.OK:
                method = result.method.value if result.method else MatchMethod.TITLE_ONLY.value
                return success(result.remote_id, outcome, method, result.score, result.matched_title, result.matched_year)
            if outcome.status is FetchStatus.API_ERROR:
                return failed(API_ERROR, outcome.message, remote_id=None)
            return failed(UNMATCHED, f"matched ID {result.remote_id} could not be retrieved", remote_id=None)
        candidates = [asdict(c) for c in getattr(result, "candidates", [])]
        if status is MatchStatus.AMBIGUOUS:
            return failed(AMBIGUOUS, result.message or "several candidates are equally likely",
                          remote_id=None, candidates=candidates)
        if status is MatchStatus.API_ERROR or status is FetchStatus.API_ERROR:
            return failed(API_ERROR, result.message, remote_id=None if invalid_id else getattr(old, "remote_id", None))
        message = getattr(result, "message", None)
        if invalid_id:
            return failed(INVALID_BINDING, f"stored ID {invalid_id} no longer exists; no new match"
                          + (f" ({message})" if message else ""), remote_id=None, candidates=candidates)
        return failed(UNMATCHED, message, remote_id=None, candidates=candidates)

    def _resolve_child(self, ap: ActivePlugin, key: tuple, series_remote: Optional[str], season: int,
                       episode: Optional[int]) -> Optional[PluginOutcome]:
        pid = ap.plugin.plugin_id
        if not series_remote:
            return None  # this plugin did not match the series
        session = self.sessions[pid]
        if episode is None:
            outcome = self._call(lambda: session.get_season(series_remote, season), pid)
        else:
            outcome = self._call(lambda: session.get_episode(series_remote, season, episode), pid)
        if outcome.status is FetchStatus.UNSUPPORTED:
            return None
        old = self.state.get_binding(pid, *key)
        common = dict(remote_parent_id=series_remote, season_number=season, episode_number=episode)
        if outcome.status is FetchStatus.OK:
            now = now_iso()
            binding = self._binding(ap, key, old, status=MATCHED, remote_id=outcome.remote_id,
                                    match_method=MatchMethod.PARENT.value, matched_at=now,
                                    last_successful_scrape=now, message=None, **common)
            return PluginOutcome(pid, MATCHED, outcome.metadata, binding)
        status = NOT_FOUND if outcome.status is FetchStatus.NOT_FOUND else API_ERROR
        message = outcome.message or ("not present at the metadata source" if status == NOT_FOUND else None)
        binding = self._binding(ap, key, old, status=status, message=message, remote_id=None, **common)
        return PluginOutcome(pid, status, None, binding, message)

    # -- movies ------------------------------------------------------------------------------------------
    def _movie(self, item: LibraryItem) -> None:
        key = ("movie", item.provider_id, item.category_id, item.item_id)
        if not self._item_needs(key, item.nfo_path):
            self.stats.skipped += 1
            return

        def outcomes(existing: MetadataResult) -> list[PluginOutcome]:
            result = []
            for ap in self.active:
                if not ap.plugin.supports(Capability.MOVIES):
                    continue
                result.append(self._resolve_root(ap, key, "movie", item.title, item.year, existing))
            return result

        def fill(meta: MetadataResult) -> None:
            if not meaningful(meta.title):
                meta.title = item.title

        self._pipeline(item.label, item.nfo_path, NfoKind.MOVIE, outcomes, fill)

    # -- series ------------------------------------------------------------------------------------------
    def _series(self, show: LibrarySeries, episodes: list[LibraryItem]) -> None:
        key = ("series", show.provider_id, show.category_id, show.series_id)
        nfo = tvshow_nfo_path(show.folder_path)
        series_remote: dict[str, Optional[str]] = {}
        if self._item_needs(key, nfo):
            def outcomes(existing: MetadataResult) -> list[PluginOutcome]:
                return [self._resolve_root(ap, key, "series", show.title, show.year, existing)
                        for ap in self.active if ap.plugin.supports(Capability.SERIES)]

            def fill(meta: MetadataResult) -> None:
                if not meaningful(meta.title):
                    meta.title = show.title

            for o in self._pipeline(show.title, nfo, NfoKind.TVSHOW, outcomes, fill):
                if o.status == MATCHED and o.binding:
                    series_remote[o.plugin_id] = o.binding.remote_id
        else:
            self.stats.skipped += 1
        self.progress.advance()
        for ap in self.active:  # series skipped or matched earlier: reuse stored bindings
            pid = ap.plugin.plugin_id
            if pid not in series_remote:
                stored = self.state.get_binding(pid, *key)
                series_remote[pid] = stored.remote_id if stored and stored.status == MATCHED else None
        series_title = self._current_title(nfo) or show.title

        by_season: dict[int, list[LibraryItem]] = defaultdict(list)
        for ep in episodes:
            by_season[ep.season or 0].append(ep)
        for season in sorted(by_season):
            self.progress.check_cancelled()
            eps = by_season[season]
            folder = os.path.dirname(eps[0].strm_path)
            season_key = ("season", show.provider_id, show.category_id, f"{show.series_id}/{season}")
            self._guard(f"{show.title} season {season}",
                        lambda k=season_key, f=folder, s=season: self._season(k, f, s, series_remote))
            self.progress.advance()
            for ep in sorted(eps, key=lambda e: e.episode or 0):
                self.progress.check_cancelled()
                self._guard(ep.label, lambda e=ep: self._episode(e, series_remote, series_title))
                self.progress.advance()

    def _current_title(self, nfo_path: str) -> Optional[str]:
        try:
            return parse_metadata(NfoDocument.load(nfo_path, NfoKind.TVSHOW), NfoKind.TVSHOW).title \
                if atomic.is_file(nfo_path) else None
        except NfoError:
            return None

    def _season(self, key: tuple, folder: str, season: int, series_remote: dict[str, Optional[str]]) -> None:
        nfo = season_nfo_path(folder)
        if not self._item_needs(key, nfo):
            self.stats.skipped += 1
            return

        def outcomes(existing: MetadataResult) -> list[PluginOutcome]:
            result = []
            for ap in self.active:
                if ap.plugin.supports(Capability.SEASONS):
                    o = self._resolve_child(ap, key, series_remote.get(ap.plugin.plugin_id), season, None)
                    if o:
                        result.append(o)
            return result

        def fill(meta: MetadataResult) -> None:
            meta.season_number = season

        self._pipeline(f"season {season}", nfo, NfoKind.SEASON, outcomes, fill)

    def _episode(self, ep: LibraryItem, series_remote: dict[str, Optional[str]], series_title: str) -> None:
        key = ("episode", ep.provider_id, ep.category_id, ep.item_id)
        if not self._item_needs(key, ep.nfo_path):
            self.stats.skipped += 1
            return
        season, number = ep.season or 0, ep.episode or 0

        def outcomes(existing: MetadataResult) -> list[PluginOutcome]:
            result = []
            for ap in self.active:
                if ap.plugin.supports(Capability.EPISODES):
                    o = self._resolve_child(ap, key, series_remote.get(ap.plugin.plugin_id), season, number)
                    if o:
                        result.append(o)
            return result

        def fill(meta: MetadataResult) -> None:
            # The local numbering is authoritative; scraping never renumbers.
            meta.season_number, meta.episode_number = season, number
            if not meaningful(meta.show_title):
                meta.show_title = series_title

        self._pipeline(ep.label, ep.nfo_path, NfoKind.EPISODE, outcomes, fill)


def _media_type(kind: NfoKind) -> MediaType:
    return {NfoKind.MOVIE: MediaType.MOVIE, NfoKind.TVSHOW: MediaType.SERIES,
            NfoKind.SEASON: MediaType.SEASON, NfoKind.EPISODE: MediaType.EPISODE}[kind]

