"""Metadata scrape jobs.

For every library item::

    existing NFO -> MetadataResult -> plugin 1 (merge) -> plugin 2 (merge) ... -> NFO written once

Matching order, per plugin (the same for every plugin, owned by the core):

1. a stored valid binding (remote ID) is fetched directly; a definitive "not found"
   invalidates it and falls through, an API error does NOT trigger a rematch;
2. an ID in the plugin's namespace already present in the NFO (``<uniqueid type=...>``);
3. the plugin's own matching (external-ID lookups, then title/year search).

IDs the user entered by hand (bindings with ``manual_ids``, see
:mod:`.manual_matching`) are the item's absolute truth. A manual binding is fetched like any
stored binding, also by forced scrapes, until the source says it no longer exists. Every
other plugin: if its own namespace has a manual value it uses exactly that ID (no matching);
a match whose external IDs contradict a manual ID is rejected; and no plugin's merge changes
a manual ID in the NFO, whatever its overwrite setting.

A confirmed manual match is followed by one rebuild: the first successful fetch through it
(``last_successful_scrape`` still empty) clears the item's descriptive NFO fields and merges
every plugin's answer from scratch, that plugin as if its overwrite switch were on; artwork
it selects replaces managed artwork, and artwork left from a replaced match (its source
plugin now matches another remote ID, or nothing) is removed. This covers the item and, for
a series, its seasons and episodes. User artwork is never touched. After that the normal
rules apply.

Series are matched once; seasons and episodes are fetched with the series' remote ID
plus the local season/episode numbers, never matched separately. Scraping never renames
files and never touches ``<fileinfo>``.

Artwork: every plugin's candidates are selected per slot with the same priority/overwrite
rules (:mod:`.artwork_selection`) and handed to the core artwork manager, which stores the
winner as a local file or a remote NFO reference. Plugins never write artwork themselves.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from functools import partial
from dataclasses import asdict, dataclass, field, replace
from datetime import timedelta
from typing import Any, Callable, Optional

from ..artwork.download import ArtworkDownloader
from ..artwork.manager import ArtworkManager, NfoEdit
from ..artwork.naming import ArtworkTarget
from ..config.settings import Settings
from ..filesystem import atomic
from ..jobs.progress import JobCancelled, JobProgress
from ..library.index import LibraryIndex
from ..library.models import EPISODE, MOVIE, LibraryItem, LibrarySeries
from ..nfo.document import NfoDocument, NfoError
from ..nfo.metadata_parser import parse_metadata
from ..nfo.metadata_writer import apply_metadata
from ..nfo.paths import NfoKind, tvshow_nfo_path
from ..nfo.service import NfoOwner, update_nfo
from ..storage.artwork import ItemKey
from ..storage.db import Database
from ..storage.nfo_files import NfoFileRepository
from ..storage.scrapers import (
    AMBIGUOUS,
    API_ERROR,
    INVALID_BINDING,
    MATCHED,
    NOT_FOUND,
    UNMATCHED,
    Binding,
    BindingItemKey,
    ScraperStateRepository,
)
from ..utils.redact import redact
from ..utils.timeutil import now_iso, older_than
from .artwork_selection import PluginArtwork, Slot, select_artwork
from .manager import ActivePlugin, ScraperManager
from .merge import descriptive_equal, meaningful, merge_into, snapshot
from .models import Artwork, ArtworkType, MediaType, MetadataResult
from .plugin import ArtworkPayload, Capability, ScraperSession
from .results import FetchOutcome, FetchStatus, MatchMethod, MatchOutcome, MatchQuery, MatchStatus

log = logging.getLogger(__name__)
MAX_MESSAGES = 500
UNSUPPORTED = "unsupported"


@dataclass
class ScrapeRequest:
    provider_id: Optional[int] = None
    kinds: tuple[str, ...] = (MOVIE, "series")
    force: bool = False
    #: Only this root item: ``(item_kind, category_id, item_id)`` of a movie or series (with its
    #: seasons and episodes) of ``provider_id``.
    item: Optional[tuple[str, str, str]] = None

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
    artwork: dict[str, int] = field(default_factory=dict)

    def warn(self, message: str) -> None:
        if len(self.warnings) < MAX_MESSAGES:
            self.warnings.append(redact(message))

    def error(self, message: str) -> None:
        self.errors_count += 1
        if len(self.errors) < MAX_MESSAGES:
            self.errors.append(redact(message))

    def counters(self) -> dict[str, int]:
        counts = {k: v for k, v in asdict(self).items() if isinstance(v, int)}
        counts.update({f"artwork_{k}": v for k, v in self.artwork.items() if k != "errors_count"})
        return counts

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
    #: The source answered in a format the plugin no longer understands (see FetchOutcome).
    format_error: bool = False
    #: A successful fetch not yet recorded as such: ``last_successful_scrape`` is only set
    #: once the item's NFO was written (or needed no write), so a failed write is retried.
    succeeded_at: Optional[str] = None


@dataclass
class _RootContext:
    """Everything the match-order steps for one root item and one plugin share."""

    ap: ActivePlugin
    key: BindingItemKey
    title: str
    session: ScraperSession
    fetch: Callable[[str], FetchOutcome]
    match: Callable[[MatchQuery], MatchOutcome]
    old: Optional[Binding]
    locked: dict[str, str]
    namespace: Optional[str]
    own_manual: bool  # this plugin's own manual match
    pinned: Optional[str]  # a manual ID another match set in this plugin's namespace
    now: str
    invalid_id: Optional[str] = None
    invalid_why: str = "no longer exists"

    @property
    def pid(self) -> str:
        return self.ap.plugin.plugin_id


class ScrapeEngine:
    def __init__(self, db: Database, settings: Settings, manager: ScraperManager, progress: JobProgress,
                 library_root: str = "", artwork_downloader: Optional[ArtworkDownloader] = None) -> None:
        self.db = db
        self.settings = settings
        self.manager = manager
        self.progress = progress
        self.state = ScraperStateRepository(db)
        self.nfo_files = NfoFileRepository(db)
        self.index = LibraryIndex(db)
        self.stats = ScrapeStats()
        self.active: list[ActivePlugin] = []
        self.sessions: dict[str, ScraperSession] = {}
        self.retry_after = timedelta(days=max(0, settings.metadata_retry_unmatched_days))
        self.force = False
        self.artwork = ArtworkManager(db, settings, library_root, downloader=artwork_downloader,
                                      private_fetch=self._private_artwork)
        # Season posters supplied with a series, per series target and plugin (this job only).
        self._season_art: dict[ItemKey, dict[str, list[Artwork]]] = {}
        # Every plugin's binding per item key, loaded once per item (cleared after each item).
        self._bindings: dict[BindingItemKey, dict[str, Binding]] = {}

    # -- entry point -------------------------------------------------------------------------------
    def run(self, request: ScrapeRequest, label: str = "") -> tuple[str, ScrapeStats]:
        """Run one scrape job; returns ``(status, stats)`` with status ``success``, ``partial``,
        ``failed`` or ``cancelled``.

        Contract:
            Preconditions:
                - One engine runs one job: ``stats``, ``sessions`` and the artwork manager are
                  not reset, and the artwork manager is closed at the end.
            Silences:
                - ``JobCancelled``: status ``cancelled`` plus a warning.
                - Any other exception escaping the job: status ``failed``, the redacted message
                  in ``stats.errors`` and a logged traceback.
                - Exceptions from ``session.close()`` (logged) and from status reporting.
                - No enabled and configured plugin gives ``failed``; item errors give ``partial``.
        """
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
            self._report_sessions()
            for session in self.sessions.values():
                try:
                    session.close()
                except Exception:
                    log.exception("Closing scraper session failed")
            self.artwork.close()
        art = self.artwork.stats
        self.stats.artwork = art.counters()
        for message in art.errors:
            self.stats.error(message)
        self.stats.errors_count += art.errors_count - len(art.errors)
        for message in art.warnings:
            self.stats.warn(message)
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
        """Start the sessions and scrape the requested movies, then the series.

        Contract:
            Raises:
                JobCancelled: From ``progress.check_cancelled`` between items (not caught).
            Silences:
                - A plugin whose ``create_session`` raises is reported as an error and left out
                  of ``self.active`` for the whole job.
                - A ``request.item`` that is no longer in the library is reported as one error;
                  the job then does nothing.
        """
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
                log.warning("Scraper %s could not start", ap.plugin.plugin_id, exc_info=True)
                self.stats.error(f"{ap.plugin.plugin_id}: could not start: {exc}")
        self.active = [ap for ap in self.active if ap.plugin.plugin_id in self.sessions]

        only = None  # one root item: (category_id, item_id), read on its own
        kinds = request.kinds
        if request.item is not None:
            kind, category_id, item_id = request.item
            only = (category_id, item_id)
            kinds = tuple(k for k in kinds if k == kind)
        movies = self.index.items(request.provider_id, kinds=(MOVIE,), item=only) if MOVIE in kinds else []
        series = list(self.index.series(request.provider_id, item=only)) if "series" in kinds else []
        if only is not None and not movies and not series:
            self.stats.error("The item is no longer in the library")
        episodes: dict[tuple[int, str, Optional[str]], list[LibraryItem]] = defaultdict(list)
        if series:
            for ep in self.index.items(request.provider_id, kinds=(EPISODE,), item=only):
                episodes[(ep.provider_id, ep.category_id, ep.series_id)].append(ep)
        seasons_total = sum(len({e.season or 0 for e in eps}) for eps in episodes.values())
        self.progress.update(phase="Scraping", total=len(movies) + len(series) + seasons_total
                             + sum(len(v) for v in episodes.values()))

        for movie in movies:
            self.progress.check_cancelled()
            self._guard(movie.label, partial(self._movie, movie))
            self.progress.advance()
        for show in series:
            self.progress.check_cancelled()
            eps = episodes.get((show.provider_id, show.category_id, show.series_id), [])
            self._guard(show.title, partial(self._series, show, eps))

    def _report_sessions(self) -> None:
        """Job-wide plugin conditions (suspension) become one warning; status is persisted."""
        for pid, session in self.sessions.items():
            if session.suspended:
                self.stats.warn(session.suspended)
            try:
                update = session.status_update()
                if update:
                    self.manager.record_status(pid, update)
            except Exception:
                log.exception("Recording the status of scraper %s failed", pid)

    def _suspended(self, plugin_id: str) -> bool:
        session = self.sessions.get(plugin_id)
        return session is None or bool(session.suspended)

    def _private_artwork(self, plugin_id: str, download_ref: str) -> Optional[ArtworkPayload]:
        """Authenticated artwork bytes from a plugin session (local downloads only)."""
        if self._suspended(plugin_id):
            return None
        return self.sessions[plugin_id].fetch_artwork(download_ref)

    def _guard(self, label: str, action: Callable[[], None]) -> None:
        """One failing item never stops the job.

        Contract:
            Raises:
                JobCancelled: Re-raised as is.
            Silences:
                - Any other exception from ``action``: counted in ``stats.errors`` and the job
                  progress, logged with traceback. Bindings saved before the failure stay.
                - The per-item bindings cache is always cleared, also on failure.
        """
        try:
            self.progress.update(current_item=label)
            action()
        except JobCancelled:
            raise
        except Exception as exc:
            self.stats.error(f"{label}: {exc}")
            self.progress.error(f"{label}: {exc}")
            log.exception("Metadata scrape failed for %s", label)
        finally:
            self._bindings.clear()
        self.progress.update(summary=self.stats.summary())

    # -- bindings cache ------------------------------------------------------------------------------
    def _item_bindings(self, key: BindingItemKey) -> dict[str, Binding]:
        """Every plugin's binding for ``key``, read with one query per item."""
        cached = self._bindings.get(key)
        if cached is None:
            cached = self._bindings[key] = {b.plugin_id: b for b in self.state.item_bindings(*key)}
        return cached

    def _get_binding(self, plugin_id: str, key: BindingItemKey) -> Optional[Binding]:
        return self._item_bindings(key).get(plugin_id)

    def _save_binding(self, binding: Binding) -> None:
        self.state.save_binding(binding)
        key: BindingItemKey = (binding.item_kind, binding.provider_id, binding.category_id, binding.item_id)
        if key in self._bindings:
            self._bindings[key][binding.plugin_id] = binding

    def _confirm_success(self, outcomes: list[PluginOutcome]) -> None:
        """Record the successful fetches of an item whose NFO is now up to date.

        Contract:
            Preconditions:
                - Called only after the item's NFO was written or needed no write.
                  ``last_successful_scrape`` then means both "the NFO is up to date" (the
                  incremental skip in :meth:`_needs`) and "the one-time rebuild after a manual
                  match is done" (:meth:`_pending_refresh`); setting it any earlier breaks both.
        """
        for o in outcomes:
            if o.succeeded_at and o.binding is not None:
                o.binding = replace(o.binding, last_successful_scrape=o.succeeded_at)
                self._save_binding(o.binding)

    # -- incremental skip ----------------------------------------------------------------------------
    def _needs(self, ap: ActivePlugin, key: BindingItemKey, nfo_exists: bool) -> bool:
        """Whether ``ap`` must look at the item again in this job.

        Contract:
            Silences:
                - No binding, a changed config fingerprint, ``api_error`` and any unknown status
                  all answer True (fail safe: the item is processed again).
                - An unparseable ``last_attempt`` counts as due (``older_than``).
        """
        if self.force:
            return True
        binding = self._get_binding(ap.plugin.plugin_id, key)
        if binding is None or binding.config_fingerprint != ap.config_fingerprint:
            return True
        if binding.status == MATCHED:
            return not (binding.last_successful_scrape and nfo_exists)
        if binding.status in (UNMATCHED, AMBIGUOUS, NOT_FOUND, INVALID_BINDING):
            return older_than(binding.last_attempt, self.retry_after)
        return True  # api_error or unknown

    def _item_needs(self, key: BindingItemKey, nfo_path: str, target: ArtworkTarget,
                    can_bind: Callable[[ActivePlugin], bool]) -> bool:
        """Whether the item must be processed in this job.

        Only plugins that can hold a binding for the item in this job are asked
        (``can_bind``: the capability, and for seasons/episodes a matched series; a suspended
        plugin records nothing). A plugin that never stores a binding for the item would
        otherwise make it due on every job, and with it every other plugin's fetches and
        searches (ignoring ``metadata_retry_unmatched_days``).
        """
        if self.force:
            return True
        exists = atomic.is_file(nfo_path)
        if any(self._needs(ap, key, exists) for ap in self.active
               if can_bind(ap) and not self._suspended(ap.plugin.plugin_id)):
            return True
        # Items scraped before artwork management existed have no artwork selection yet.
        return not self.artwork.was_evaluated(target)

    # -- artwork ----------------------------------------------------------------------------------------
    def _candidates(self, target: ArtworkTarget, art: Optional[list[Artwork]]) -> list[Artwork]:
        """Candidates that belong to ``target`` (season posters get the local season number).

        Contract:
            Raises:
                ValueError: A candidate whose ``type`` is not an :class:`ArtworkType` value
                    (implicit, ``ArtworkType(a.type)``); the whole item then fails in
                    :meth:`_guard` instead of the candidate being dropped.
        """
        result: list[Artwork] = []
        for a in art or []:
            kind = ArtworkType(a.type)
            if target.item_kind == "season":
                if kind in (ArtworkType.POSTER, ArtworkType.SEASON_POSTER) and a.season_number in (None, target.season):
                    result.append(Artwork(**{**asdict(a), "type": ArtworkType.SEASON_POSTER,
                                             "season_number": target.season}))
            elif target.item_kind == "episode":
                if kind is ArtworkType.EPISODE_STILL:
                    result.append(a)
            elif kind not in (ArtworkType.SEASON_POSTER, ArtworkType.EPISODE_STILL, ArtworkType.PERSON_IMAGE):
                result.append(a)
        return result

    def _select_artwork(self, target: ArtworkTarget, outcomes: list[PluginOutcome],
                        extra: dict[str, list[Artwork]], refreshed: frozenset[str] = frozenset()) -> dict[Slot, Artwork]:
        """The winning artwork per slot for ``target`` (see :func:`.artwork_selection.select_artwork`).

        Contract:
            Preconditions:
                - Only ``MATCHED`` outcomes with metadata count as "reported"; ``extra`` (season
                  posters supplied with the series) is added for every active plugin.
            Raises:
                ValueError: An unknown artwork type (see :meth:`_candidates`).
            Side effects:
                - For a series target, remembers each plugin's season posters in
                  ``self._season_art`` for its seasons in this job.
        """
        per_plugin: list[PluginArtwork] = []
        reported: set[str] = set()
        season_art: dict[str, list[Artwork]] = {}
        for ap in self.active:
            pid = ap.plugin.plugin_id
            outcome = next((o for o in outcomes if o.plugin_id == pid), None)
            candidates: list[Artwork] = []
            remote_id = None
            if outcome is not None and outcome.status == MATCHED and outcome.metadata is not None:
                reported.add(pid)
                art = outcome.metadata.artwork
                candidates = self._candidates(target, art)
                remote_id = outcome.binding.remote_id if outcome.binding else None
                if target.item_kind == "series":
                    season_art[pid] = [a for a in art or [] if ArtworkType(a.type) is ArtworkType.SEASON_POSTER]
            candidates += self._candidates(target, extra.get(pid))
            per_plugin.append(PluginArtwork(pid, ap.state.overwrite or pid in refreshed, candidates, ap.config.get("language"), remote_id))
        if target.item_kind == "series":
            self._season_art[target.key] = season_art
        return select_artwork(per_plugin, self.artwork.stored_selection(target), reported)

    # -- the pipeline ---------------------------------------------------------------------------------
    def _manual_locks(self, key: BindingItemKey) -> dict[str, str]:
        """The IDs the user set by hand for a root item (installed plugins' valid manual bindings).

        Contract:
            Preconditions:
                - Reads the per-item bindings cache: call it again after bindings changed
                  (``_pipeline`` does, so a manual ID that vanished during the job is not
                  written back to the NFO).
            Silences:
                - Bindings of uninstalled plugins and bindings not ``matched``/``api_error`` are
                  ignored; the first plugin (by ID) wins a namespace both set.
        """
        locks: dict[str, str] = {}
        for binding in sorted(self._item_bindings(key).values(), key=lambda b: b.plugin_id):
            if binding.manual_ids and binding.status in (MATCHED, API_ERROR) \
                    and self.manager.registry.get(binding.plugin_id) is not None:
                for namespace, value in binding.manual_ids.items():
                    locks.setdefault(namespace.strip().lower(), value)
        return locks

    def _pending_refresh(self, key: BindingItemKey) -> frozenset[str]:
        """Active plugins whose manual match was confirmed but not yet fetched successfully."""
        pending: set[str] = set()
        for ap in self.active:
            binding = self._get_binding(ap.plugin.plugin_id, key)
            if binding and binding.manual_ids and binding.status in (MATCHED, API_ERROR) \
                    and not binding.last_successful_scrape:
                pending.add(ap.plugin.plugin_id)
        return frozenset(pending)

    def _pipeline(self, label: str, nfo_path: str, kind: NfoKind,
                  outcomes_for: Callable[[MetadataResult, dict[str, str]], list[PluginOutcome]],
                  local_fill: Callable[[MetadataResult], None], target: ArtworkTarget,
                  extra_art: Optional[dict[str, list[Artwork]]] = None,
                  root_key: Optional[BindingItemKey] = None,
                  refresh: frozenset[str] = frozenset()) -> tuple[list[PluginOutcome], frozenset[str]]:
        """Returns the outcomes and the plugins whose one-time refresh after a manual match was
        applied (``refresh`` restricted to the plugins that matched).

        Contract:
            Preconditions:
                - ``root_key`` only for movies and series: it enables the manual-ID locks.
                  Seasons and episodes pass none and get their ``refresh`` from the series.
                - ``outcomes_for`` gets the NFO's IDs overlaid with the locks; the locks are
                  read again afterwards for the NFO, because a manual ID whose source said
                  "not found" during the call is no longer one.
            Raises:
                sqlite3.Error: From the binding and artwork repositories (not caught; the item
                    fails in :meth:`_guard`).
                ValueError: An unknown artwork type from a plugin (see :meth:`_candidates`).
            Silences:
                - An existing NFO that cannot be read: one error, nothing fetched or written,
                  returns ``([], frozenset())``.
                - A failed NFO write (:meth:`_write_nfo`): the fetches are not confirmed, so the
                  next normal run processes the item again; bindings were already saved.
        """
        exists = atomic.is_file(nfo_path)
        existing = MetadataResult(media_type=_media_type(kind))
        if exists:
            try:
                doc = NfoDocument.load(nfo_path, kind)
            except NfoError as exc:
                self.stats.error(f"{label}: existing NFO cannot be read, left untouched: {exc}")
                return [], frozenset()
            existing = parse_metadata(doc, kind)
        existing.artwork = None  # artwork references are owned by the artwork manager
        result = snapshot(existing)
        locked = self._manual_locks(root_key) if root_key else {}
        lookup = snapshot(existing)  # what plugins match with: manual IDs win over the NFO's
        lookup.external_ids.update(locked)
        outcomes = outcomes_for(lookup, locked)
        locked = self._manual_locks(root_key) if root_key else {}  # a vanished manual ID is no longer one
        result.external_ids.update(locked)
        refreshed = frozenset(o.plugin_id for o in outcomes
                              if o.plugin_id in refresh and o.status == MATCHED and o.metadata is not None)
        if refreshed:  # rebuild: nothing of the old (wrong) match survives, the locked IDs do
            result = MetadataResult(media_type=_media_type(kind), external_ids=dict(locked))
        self._merge_outcomes(result, outcomes, kind, locked, refreshed)
        local_fill(result)
        self._count(label, outcomes)

        # Artwork: select per slot, let the artwork manager download/plan before the NFO write.
        selections = self._select_artwork(target, outcomes, extra_art or {}, refreshed)
        retire = self._stale_artwork(target, outcomes, selections) if refreshed else frozenset()
        plan = self.artwork.prepare(target, selections, force=bool(refreshed), retire=retire)
        own_edit = plan.edit_for(nfo_path)
        describe = bool(refreshed) or \
            (not descriptive_equal(result, existing) and (exists or any(o.status == MATCHED for o in outcomes)))
        if not describe:
            self.stats.unchanged += 1
        written: dict[str, bool] = {}
        if describe or own_edit is not None:
            written[nfo_path] = self._write_nfo(label, nfo_path, kind, target, result if describe else None,
                                                own_edit, clear_missing=bool(refreshed))
        self.artwork.complete(plan, written)
        self.artwork.mark_evaluated(target)
        if written.get(nfo_path, True):
            self._confirm_success(outcomes)
        return outcomes, refreshed

    def _merge_outcomes(self, result: MetadataResult, outcomes: list[PluginOutcome], kind: NfoKind,
                        locked: dict[str, str], refreshed: frozenset[str]) -> None:
        """Merge every matched plugin's answer into ``result``, strictly in priority order.

        Contract:
            Side effects:
                - For movies and series, adds the plugin's own remote ID under its namespace to
                  the outcome's ``metadata.external_ids`` when the plugin did not list it.
            Preconditions:
                - ``locked`` keys are lower-case namespaces (as :meth:`_manual_locks` returns).
        """
        for ap in self.active:
            outcome = next((o for o in outcomes if o.plugin_id == ap.plugin.plugin_id), None)
            if outcome is None or outcome.status != MATCHED or outcome.metadata is None:
                continue
            meta = outcome.metadata
            namespace = ap.plugin.id_namespace
            if namespace and outcome.binding and outcome.binding.remote_id and namespace not in meta.external_ids \
                    and kind in (NfoKind.MOVIE, NfoKind.TVSHOW):
                meta.external_ids[namespace] = outcome.binding.remote_id
            merge_into(result, meta, overwrite=ap.state.overwrite or ap.plugin.plugin_id in refreshed,
                       locked_ids=locked.keys())

    def _write_nfo(self, label: str, nfo_path: str, kind: NfoKind, target: ArtworkTarget,
                   result: Optional[MetadataResult], own_edit: Optional[NfoEdit], clear_missing: bool) -> bool:
        """Write the merged descriptive fields (``result``, if any) and the artwork manager's
        edit in one NFO update. Returns whether the write succeeded.

        Contract:
            Silences:
                - ``NfoError`` (an unreadable NFO is never overwritten) and ``OSError``: one
                  error in the stats, returns False.
            Raises:
                sqlite3.Error: From NFO ownership bookkeeping (``NfoOwner``), not caught.
        """
        def mutate(doc: NfoDocument) -> None:
            if result is not None:
                apply_metadata(doc, result, clear_missing=clear_missing)
            if own_edit is not None:
                own_edit.apply(doc)

        owner = NfoOwner(self.nfo_files, target.key) if nfo_path == target.nfo_path else None
        try:
            update_nfo(nfo_path, kind, mutate, owner=owner)
        except (NfoError, OSError) as exc:
            self.stats.error(f"{label}: NFO write failed: {exc}")
            return False
        if result is not None:
            self.stats.updated += 1
        return True

    def _stale_artwork(self, target: ArtworkTarget, outcomes: list[PluginOutcome],
                       selections: dict[Slot, Artwork]) -> frozenset[ArtworkType]:
        """Stored artwork without a new winner whose source plugin now matches a different
        remote ID, or no longer matches at all (not when it failed or did not run)."""
        by_plugin = {o.plugin_id: o for o in outcomes}
        selected = {art_type for art_type, _ in selections}
        stale: set[ArtworkType] = set()
        for (art_type, _), old in self.artwork.stored_selection(target).items():
            o = by_plugin.get(old.source_plugin or "")
            if art_type in selected or o is None:
                continue
            if o.status in (UNMATCHED, AMBIGUOUS, NOT_FOUND, INVALID_BINDING) or (
                    o.status == MATCHED and o.binding is not None and not _same(o.binding.remote_id, old.source_remote_id)):
                stale.add(art_type)
        return frozenset(stale)

    def _count(self, label: str, outcomes: list[PluginOutcome]) -> None:
        """Count one processed item (matched if any plugin matched) and report API errors.

        Contract:
            Silences:
                - An item whose outcomes are only ``api_error`` / ``unsupported`` (or none) is
                  ``considered`` but counted in no other bucket.
        """
        self.stats.considered += 1
        statuses = {o.status for o in outcomes}
        for o in outcomes:
            if o.status == API_ERROR:
                prefix = "unexpected answer format, existing match kept: " if o.format_error else ""
                self.stats.error(f"{label} [{o.plugin_id}]: {prefix}{o.message or 'request failed'}")
        if MATCHED in statuses:  # one plugin's match is enough
            self.stats.matched += 1
        elif AMBIGUOUS in statuses:  # uncertain is unmatched; "ambiguous" counts how many of those
            self.stats.unmatched += 1
            self.stats.ambiguous += 1
        elif NOT_FOUND in statuses:
            self.stats.not_found += 1
        elif statuses & {UNMATCHED, INVALID_BINDING}:
            self.stats.unmatched += 1

    # -- bindings --------------------------------------------------------------------------------------
    def _binding(self, ap: ActivePlugin, key: BindingItemKey, old: Optional[Binding], **values: Any) -> Binding:
        """Save and return ``old`` updated with ``values`` (a new row when ``old`` is None).

        Contract:
            Preconditions:
                - Every field not in ``values`` is carried over from ``old`` unchanged (for
                  example ``matched_title`` or ``last_successful_scrape``); callers reset what
                  must not survive. ``last_attempt`` and the config fingerprint are always set.
            Raises:
                sqlite3.Error: From the repository (not caught).
        """
        base: dict[str, Any] = asdict(old) if old else {"plugin_id": ap.plugin.plugin_id, "item_kind": key[0],
                                          "provider_id": key[1], "category_id": key[2], "item_id": key[3],
                                          "status": UNMATCHED}
        base.update(values, last_attempt=now_iso(), config_fingerprint=ap.config_fingerprint)
        binding = Binding(**base)
        self._save_binding(binding)
        return binding

    def _call(self, fn: Callable[[], Any], plugin_id: str, on_error: Callable[[str], Any] = FetchOutcome.error):
        """Plugin calls are isolated: an exception becomes an API error outcome of the type the
        call returns (``on_error``: ``FetchOutcome.error`` or ``MatchOutcome.error``).

        Contract:
            Silences:
                - Every ``Exception`` from ``fn`` (logged redacted with traceback). A plugin
                  that returns ``None`` instead of an outcome is not caught here.
        """
        try:
            return fn()
        except Exception as exc:
            log.warning("Scraper %s raised: %s", plugin_id, redact(exc), exc_info=True)
            return on_error(redact(f"plugin error: {exc}"))

    def _resolve_root(self, ap: ActivePlugin, key: BindingItemKey, media: str, title: str, year: Optional[int],
                      existing: MetadataResult, found_ids: Optional[dict[str, str]] = None,
                      locked: Optional[dict[str, str]] = None) -> PluginOutcome:
        """Resolve one root item for one plugin, following the match order of the module
        docstring: stored binding, a manual ID pinned by another match, the NFO's (or a
        higher-priority plugin's) ID, then the plugin's own matching.

        ``found_ids``: external IDs higher-priority plugins supplied for this item in this job.
        They are used like NFO IDs (after them), so e.g. an IMDb ID found by one plugin spares
        another plugin a title search. ``locked``: the IDs the user set by hand for the item.

        Contract:
            Preconditions:
                - The steps run in this order on purpose: :meth:`_try_stored` records a dead
                  stored ID in ``ctx.invalid_id`` (and drops a vanished manual ID from
                  ``ctx.locked``), and the later steps read those fields to avoid it.
            Silences:
                - A suspended plugin answers ``unsupported`` and nothing is recorded.
        """
        pid = ap.plugin.plugin_id
        if self._suspended(pid):
            return PluginOutcome(pid, UNSUPPORTED)  # skipped for this job; nothing recorded
        session = self.sessions[pid]
        old = self._get_binding(pid, key)
        locked = locked or {}
        namespace = ap.plugin.id_namespace
        own_manual = bool(old and old.manual_ids and old.remote_id and old.status in (MATCHED, API_ERROR))
        ctx = _RootContext(
            ap=ap, key=key, title=title, session=session,
            fetch=session.get_movie if media == "movie" else session.get_series,
            match=session.match_movie if media == "movie" else session.match_series,
            old=old, locked=locked, namespace=namespace, own_manual=own_manual,
            pinned=locked.get(namespace) if namespace and not own_manual else None, now=now_iso())
        found_ids = found_ids or {}
        return (self._try_stored(ctx)
                or self._try_pinned(ctx)
                or self._try_known_id(ctx, existing, found_ids)
                or self._try_match(ctx, media, title, year, existing, found_ids))

    def _root_success(self, ctx: _RootContext, remote_id: str, outcome: FetchOutcome, method: str,
                      score: Optional[float] = None, mtitle: Optional[str] = None,
                      myear: Optional[int] = None) -> PluginOutcome:
        """Save a ``matched`` binding for ``remote_id``; its success is confirmed only after the
        NFO write (``succeeded_at``).

        Contract:
            Preconditions:
                - The plugin's own ``manual_ids`` survive only when this is its own manual
                  match, its ID was not invalidated in this job and ``remote_id`` is that ID.
        """
        old = ctx.old
        keep_manual = ctx.own_manual and ctx.invalid_id is None and old is not None and remote_id == old.remote_id
        binding = self._binding(ctx.ap, ctx.key, old, status=MATCHED, remote_id=remote_id, match_method=method,
                                match_score=score,
                                matched_title=mtitle or (outcome.metadata.title if outcome.metadata else None),
                                matched_year=myear or (outcome.metadata.year if outcome.metadata else None),
                                matched_at=old.matched_at if (old and old.remote_id == remote_id and old.matched_at)
                                else ctx.now,
                                last_successful_scrape=old.last_successful_scrape if old else None,
                                message=None, candidates=[],
                                manual_ids=old.manual_ids if keep_manual and old else {})
        return PluginOutcome(ctx.pid, MATCHED, outcome.metadata, binding, succeeded_at=ctx.now)

    def _root_failed(self, ctx: _RootContext, status: str, message: Optional[str], format_error: bool = False,
                     **extra: Any) -> PluginOutcome:
        """Save a binding with a failure ``status``.

        Contract:
            Preconditions:
                - The binding's ``manual_ids`` are cleared, except for the plugin's own manual
                  match answering ``api_error`` (a manual match ends only when its ID is gone).
                  Fields not in ``extra`` (such as ``remote_id``) are carried over.
        """
        if not (ctx.own_manual and ctx.invalid_id is None and status == API_ERROR):
            extra.setdefault("manual_ids", {})  # a manual match ends only when its ID is gone
        binding = self._binding(ctx.ap, ctx.key, ctx.old, status=status, message=message, **extra)
        return PluginOutcome(ctx.pid, status, None, binding, message, format_error=format_error)

    def _try_stored(self, ctx: _RootContext) -> Optional[PluginOutcome]:
        """1. The stored binding (not when it differs from an ID the user set by hand).

        Contract:
            Side effects:
                - Returns None to fall through, after setting ``ctx.invalid_id`` /
                  ``ctx.invalid_why`` when the stored ID is gone or contradicts a manual ID,
                  and narrowing ``ctx.locked`` when the plugin's own manual ID is gone.
            Silences:
                - ``api_error`` keeps the stored ID and never rematches.
        """
        old = ctx.old
        if not (old and old.remote_id and old.status in (MATCHED, API_ERROR)
                and (not ctx.pinned or _same(ctx.pinned, old.remote_id))):
            return None
        remote_id = old.remote_id
        outcome = self._call(lambda: ctx.fetch(remote_id), ctx.pid)
        if outcome.status is FetchStatus.OK:
            clash = None if ctx.own_manual else _contradiction(outcome.metadata, ctx.locked)
            if clash is None:
                return self._root_success(ctx, remote_id, outcome,
                                          old.match_method or MatchMethod.STORED_BINDING.value,
                                          old.match_score, old.matched_title, old.matched_year)
            ctx.invalid_id, ctx.invalid_why = remote_id, f"does not have the {clash} ID set by hand"
            log.info("%s: stored %s binding %s %s; matching again", ctx.title, ctx.pid, remote_id, ctx.invalid_why)
        elif outcome.status is FetchStatus.UNSUPPORTED:
            return PluginOutcome(ctx.pid, UNSUPPORTED)
        elif outcome.status is FetchStatus.API_ERROR:  # keep remote_id; never rematch on errors
            return self._root_failed(ctx, API_ERROR, outcome.message, outcome.format_error)
        else:
            ctx.invalid_id = remote_id
            log.info("%s: stored %s binding %s no longer exists; matching again", ctx.title, ctx.pid, remote_id)
            if ctx.own_manual:  # the source dropped the ID set by hand: it no longer binds anything
                ctx.locked = {ns: v for ns, v in ctx.locked.items() if not _same(old.manual_ids.get(ns), v)}
        return None

    def _try_pinned(self, ctx: _RootContext) -> Optional[PluginOutcome]:
        """1b. The user set this plugin's ID by hand (through another plugin): only that ID is used."""
        pinned = ctx.pinned
        if not pinned:
            return None
        if _same(pinned, ctx.invalid_id):
            outcome = FetchOutcome.not_found()
        else:
            outcome = self._call(lambda: ctx.fetch(pinned), ctx.pid)
        if outcome.status is FetchStatus.OK:
            return self._root_success(ctx, pinned, outcome, MatchMethod.EXTERNAL_ID.value)
        if outcome.status is FetchStatus.UNSUPPORTED:
            return PluginOutcome(ctx.pid, UNSUPPORTED)
        if outcome.status is FetchStatus.API_ERROR:
            return self._root_failed(ctx, API_ERROR, outcome.message, outcome.format_error, remote_id=None)
        return self._root_failed(ctx, UNMATCHED, f"the {ctx.namespace} ID {pinned} set by hand was not found"
                                 + (f" ({outcome.message})" if outcome.message else ""), remote_id=None, candidates=[])

    def _try_known_id(self, ctx: _RootContext, existing: MetadataResult,
                      found_ids: dict[str, str]) -> Optional[PluginOutcome]:
        """2. An ID already present in the NFO for this plugin's namespace (or supplied by a
        higher-priority plugin during this job).

        Contract:
            Preconditions:
                - ``existing`` is the NFO overlaid with the manual-ID locks, so a lock in this
                  namespace also counts as "known" (it reaches this step only through
                  :meth:`_try_pinned`, which handles it first).
            Silences:
                - A known ID that is not found, or whose metadata contradicts a manual ID,
                  falls through to :meth:`_try_match` (returns None) without a binding.
        """
        namespace = ctx.namespace
        nfo_id = existing.external_ids.get(namespace) if namespace else None
        method = MatchMethod.NFO_UNIQUEID
        if namespace and (not nfo_id or _same(nfo_id, ctx.invalid_id)) and found_ids.get(namespace):
            nfo_id, method = found_ids[namespace], MatchMethod.EXTERNAL_ID
        if not nfo_id or _same(nfo_id, ctx.invalid_id):
            return None
        known_id = nfo_id
        outcome = self._call(lambda: ctx.fetch(known_id), ctx.pid)
        if outcome.status is FetchStatus.OK and _contradiction(outcome.metadata, ctx.locked) is None:
            return self._root_success(ctx, known_id, outcome, method.value)
        if outcome.status is FetchStatus.API_ERROR:
            return self._root_failed(ctx, API_ERROR, outcome.message, outcome.format_error, remote_id=None)
        if outcome.status is FetchStatus.UNSUPPORTED:
            return PluginOutcome(ctx.pid, UNSUPPORTED)
        return None

    def _try_match(self, ctx: _RootContext, media: str, title: str, year: Optional[int],
                   existing: MetadataResult, found_ids: dict[str, str]) -> PluginOutcome:
        """3./4. The plugin's own matching (external IDs, then title/year).

        Contract:
            Silences:
                - A plugin that returns ``None`` from ``match_*`` is treated as ``unmatched``.
                - A match on the stored ID invalidated in this job is not fetched again; the
                  binding becomes ``invalid_binding``.
                - A matched ID whose fetch is not found, or whose metadata contradicts a manual
                  ID, becomes ``unmatched`` (no second candidate is tried).
        """
        invalid_id = ctx.invalid_id
        known = {**found_ids, **existing.external_ids}
        query = MatchQuery(media_type=media, title=title, year=year,
                           external_ids={k: v for k, v in known.items() if k != ctx.namespace or not _same(v, invalid_id)})
        result = self._call(lambda: ctx.match(query), ctx.pid, on_error=MatchOutcome.error)
        if result is None:  # a plugin that answers nothing has found nothing
            result = MatchOutcome.unmatched()
        status = result.status
        if status is MatchStatus.UNSUPPORTED:
            return PluginOutcome(ctx.pid, UNSUPPORTED)
        if status is MatchStatus.MATCHED and result.remote_id and not _same(result.remote_id, invalid_id):
            remote_id = result.remote_id
            outcome = self._call(lambda: ctx.fetch(remote_id), ctx.pid)
            if outcome.status is FetchStatus.OK:
                clash = _contradiction(outcome.metadata, ctx.locked)
                if clash is not None:
                    return self._root_failed(ctx, UNMATCHED,
                                             f"the match {remote_id} does not have the {clash} ID set by hand",
                                             remote_id=None)
                method = result.method.value if result.method else MatchMethod.TITLE_ONLY.value
                return self._root_success(ctx, remote_id, outcome, method, result.score, result.matched_title,
                                          result.matched_year)
            if outcome.status is FetchStatus.API_ERROR:
                return self._root_failed(ctx, API_ERROR, outcome.message, outcome.format_error, remote_id=None)
            return self._root_failed(ctx, UNMATCHED, f"matched ID {remote_id} could not be retrieved", remote_id=None)
        candidates = [asdict(c) for c in result.candidates]
        if status is MatchStatus.AMBIGUOUS:
            return self._root_failed(ctx, AMBIGUOUS, result.message or "several candidates are equally likely",
                                     remote_id=None, candidates=candidates)
        if status is MatchStatus.API_ERROR:
            return self._root_failed(ctx, API_ERROR, result.message, result.format_error,
                                     remote_id=None if invalid_id else getattr(ctx.old, "remote_id", None))
        message = result.message
        if invalid_id:
            return self._root_failed(ctx, INVALID_BINDING, f"stored ID {invalid_id} {ctx.invalid_why}; no new match"
                                     + (f" ({message})" if message else ""), remote_id=None, candidates=candidates)
        return self._root_failed(ctx, UNMATCHED, message, remote_id=None, candidates=candidates)

    def _resolve_child(self, ap: ActivePlugin, key: BindingItemKey, series_remote: Optional[str], season: int,
                       episode: Optional[int]) -> Optional[PluginOutcome]:
        """Fetch a season (``episode`` None) or an episode through the series' remote ID.

        Contract:
            Silences:
                - Returns None, recording nothing, when the plugin did not match the series, is
                  suspended, or answers ``unsupported``.
        """
        pid = ap.plugin.plugin_id
        if not series_remote or self._suspended(pid):
            return None  # this plugin did not match the series, or is skipped for this job
        session = self.sessions[pid]
        if episode is None:
            outcome = self._call(lambda: session.get_season(series_remote, season), pid)
        else:
            outcome = self._call(lambda: session.get_episode(series_remote, season, episode), pid)
        if outcome.status is FetchStatus.UNSUPPORTED:
            return None
        old = self._get_binding(pid, key)
        common = dict(remote_parent_id=series_remote, season_number=season, episode_number=episode)
        if outcome.status is FetchStatus.OK:
            now = now_iso()
            binding = self._binding(ap, key, old, status=MATCHED, remote_id=outcome.remote_id,
                                    match_method=MatchMethod.PARENT.value, matched_at=now,
                                    last_successful_scrape=old.last_successful_scrape if old else None,
                                    message=None, **common)
            return PluginOutcome(pid, MATCHED, outcome.metadata, binding, succeeded_at=now)
        status = NOT_FOUND if outcome.status is FetchStatus.NOT_FOUND else API_ERROR
        message = outcome.message or ("not present at the metadata source" if status == NOT_FOUND else None)
        binding = self._binding(ap, key, old, status=status, message=message, remote_id=None, **common)
        return PluginOutcome(pid, status, None, binding, message, format_error=outcome.format_error)

    # -- movies ------------------------------------------------------------------------------------------
    def _movie(self, item: LibraryItem) -> None:
        key: BindingItemKey = ("movie", item.provider_id, item.category_id, item.item_id)
        target = ArtworkTarget.for_movie(item)
        if not self._item_needs(key, item.nfo_path, target, lambda ap: ap.plugin.supports(Capability.MOVIES)):
            self.stats.skipped += 1
            return

        def outcomes(existing: MetadataResult, locked: dict[str, str]) -> list[PluginOutcome]:
            result: list[PluginOutcome] = []
            found: dict[str, str] = {}
            for ap in self.active:
                if not ap.plugin.supports(Capability.MOVIES):
                    continue
                result.append(self._resolve_root(ap, key, "movie", item.title, item.year, existing, found, locked))
                _collect_ids(found, result[-1])
            return result

        def fill(meta: MetadataResult) -> None:
            if not meaningful(meta.title):
                meta.title = item.title

        self._pipeline(item.label, item.nfo_path, NfoKind.MOVIE, outcomes, fill, target, root_key=key,
                       refresh=self._pending_refresh(key))

    # -- series ------------------------------------------------------------------------------------------
    def _series(self, show: LibrarySeries, episodes: list[LibraryItem]) -> None:
        """Scrape a series, then its seasons and episodes (each guarded on its own).

        Contract:
            Raises:
                JobCancelled: Between children (not caught).
            Silences:
                - A season or episode that fails is reported and the next one continues.
                - The one-time rebuild after a manual series match (``refresh``) reaches only
                  the children processed in this run: the series binding's
                  ``last_successful_scrape`` is set once ``tvshow.nfo`` is written, so a child
                  that fails or is not reached (cancel) is later merged with the normal rules.
        """
        key: BindingItemKey = ("series", show.provider_id, show.category_id, show.series_id)
        nfo = tvshow_nfo_path(show.folder_path)
        series_remote: dict[str, Optional[str]] = {}
        refreshed: frozenset[str] = frozenset()
        show_target = ArtworkTarget.for_series(show)
        if self._item_needs(key, nfo, show_target, lambda ap: ap.plugin.supports(Capability.SERIES)):
            def outcomes(existing: MetadataResult, locked: dict[str, str]) -> list[PluginOutcome]:
                result: list[PluginOutcome] = []
                found: dict[str, str] = {}
                for ap in self.active:
                    if ap.plugin.supports(Capability.SERIES):
                        result.append(self._resolve_root(ap, key, "series", show.title, show.year, existing, found,
                                                         locked))
                        _collect_ids(found, result[-1])
                return result

            def fill(meta: MetadataResult) -> None:
                if not meaningful(meta.title):
                    meta.title = show.title

            show_outcomes, refreshed = self._pipeline(show.title, nfo, NfoKind.TVSHOW, outcomes, fill, show_target,
                                                      root_key=key, refresh=self._pending_refresh(key))
            for o in show_outcomes:
                if o.status == MATCHED and o.binding:
                    series_remote[o.plugin_id] = o.binding.remote_id
        else:
            self.stats.skipped += 1
        self.progress.advance()
        for ap in self.active:  # series skipped or matched earlier: reuse stored bindings
            pid = ap.plugin.plugin_id
            if pid not in series_remote:
                stored = self._get_binding(pid, key)
                series_remote[pid] = stored.remote_id if stored and stored.status == MATCHED else None
        series_title = self._current_title(nfo) or show.title

        by_season: dict[int, list[LibraryItem]] = defaultdict(list)
        for ep in episodes:
            by_season[ep.season or 0].append(ep)
        for season in sorted(by_season):
            self.progress.check_cancelled()
            eps = by_season[season]
            folder = os.path.dirname(eps[0].strm_path)
            season_key: BindingItemKey = ("season", show.provider_id, show.category_id, f"{show.series_id}/{season}")
            season_target = ArtworkTarget.for_season(show, folder, season)
            self._guard(f"{show.title} season {season}",
                        partial(self._season, season_key, season_target, season, series_remote, refreshed))
            self.progress.advance()
            for ep in sorted(eps, key=lambda e: e.episode or 0):
                self.progress.check_cancelled()
                self._guard(ep.label, partial(self._episode, ep, series_remote, series_title, refreshed))
                self.progress.advance()

    def _current_title(self, nfo_path: str) -> Optional[str]:
        if not atomic.is_file(nfo_path):
            return None
        try:
            doc = NfoDocument.load(nfo_path, NfoKind.TVSHOW)
        except NfoError:
            return None
        return parse_metadata(doc, NfoKind.TVSHOW).title

    def _season(self, key: BindingItemKey, target: ArtworkTarget, season: int, series_remote: dict[str, Optional[str]],
                refresh: frozenset[str] = frozenset()) -> None:
        """Scrape one season.

        Contract:
            Preconditions:
                - ``target.key[3]`` is ``"<series_id>/<season>"``: the series artwork key (for
                  the season posters supplied with the series) is derived from it.
        """
        nfo = target.nfo_path
        if not refresh and not self._item_needs(key, nfo, target, _child_of(series_remote, Capability.SEASONS)):
            self.stats.skipped += 1
            return

        def outcomes(existing: MetadataResult, locked: dict[str, str]) -> list[PluginOutcome]:
            result: list[PluginOutcome] = []
            for ap in self.active:
                if ap.plugin.supports(Capability.SEASONS):
                    o = self._resolve_child(ap, key, series_remote.get(ap.plugin.plugin_id), season, None)
                    if o:
                        result.append(o)
            return result

        def fill(meta: MetadataResult) -> None:
            meta.season_number = season

        show_key: ItemKey = (target.key[0], "series", target.key[2], target.key[3].rsplit("/", 1)[0])
        self._pipeline(f"season {season}", nfo, NfoKind.SEASON, outcomes, fill, target,
                       self._season_art.get(show_key), refresh=refresh)

    def _episode(self, ep: LibraryItem, series_remote: dict[str, Optional[str]], series_title: str,
                 refresh: frozenset[str] = frozenset()) -> None:
        key: BindingItemKey = ("episode", ep.provider_id, ep.category_id, ep.item_id)
        target = ArtworkTarget.for_episode(ep)
        if not refresh and not self._item_needs(key, ep.nfo_path, target,
                                                _child_of(series_remote, Capability.EPISODES)):
            self.stats.skipped += 1
            return
        season, number = ep.season or 0, ep.episode or 0

        def outcomes(existing: MetadataResult, locked: dict[str, str]) -> list[PluginOutcome]:
            result: list[PluginOutcome] = []
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

        self._pipeline(ep.label, ep.nfo_path, NfoKind.EPISODE, outcomes, fill, target, refresh=refresh)


def _child_of(series_remote: dict[str, Optional[str]], capability: Capability) -> Callable[[ActivePlugin], bool]:
    """Plugins that fetch a season or episode in this job: they have the capability and matched
    the series (see :meth:`ScrapeEngine._resolve_child`)."""
    return lambda ap: ap.plugin.supports(capability) and bool(series_remote.get(ap.plugin.plugin_id))


def _collect_ids(found: dict[str, str], outcome: PluginOutcome) -> None:
    """Remember the external IDs a matched plugin supplied (first plugin wins per namespace)."""
    if outcome.status == MATCHED and outcome.metadata is not None:
        for namespace, value in outcome.metadata.external_ids.items():
            key, value = (namespace or "").strip().lower(), str(value or "").strip()
            if key and value and key not in found:
                found[key] = value


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return a is not None and b is not None and str(a).strip().casefold() == str(b).strip().casefold()


def _contradiction(meta: Optional[MetadataResult], locked: dict[str, str]) -> Optional[str]:
    """The first namespace in which ``meta`` lists a different ID than the one the user set by hand.

    Contract:
        Preconditions:
            - ``locked`` keys are lower-case namespaces; ``meta``'s keys are lower-cased here.
              A lock in an upper-case namespace would never match and never contradict.
    """
    if meta is None or not locked:
        return None
    ids = {(k or "").strip().lower(): str(v) for k, v in meta.external_ids.items() if v not in (None, "")}
    for namespace, value in locked.items():
        if namespace in ids and not _same(ids[namespace], value):
            return namespace
    return None


def _media_type(kind: NfoKind) -> MediaType:
    return {NfoKind.MOVIE: MediaType.MOVIE, NfoKind.TVSHOW: MediaType.SERIES,
            NfoKind.SEASON: MediaType.SEASON, NfoKind.EPISODE: MediaType.EPISODE}[kind]

