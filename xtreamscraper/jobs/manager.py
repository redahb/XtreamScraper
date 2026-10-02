"""Background sync/probe/metadata/artwork jobs.

Each job runs in its own thread so the web server stays responsive. A per-provider lock
guarantees that one provider is never synchronized (or probed) by two jobs at once;
different providers may be processed concurrently.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from collections import OrderedDict
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

from ..artwork.reconcile import ArtworkReconciler, effective_root
from ..config.paths import AppPaths
from ..config.settings import Settings, SettingsStore
from ..filesystem.layout import provider_folder_names
from ..library.models import EPISODE, MOVIE
from ..probe.engine import ProbeEngine, ProbeRequest, probe_summary_dict
from ..probe.ffprobe_runner import locate_ffprobe
from ..storage.db import Database
from ..metadata.engine import ScrapeEngine, ScrapeRequest
from ..metadata.manager import ScraperManager
from ..storage.history import (
    ArtworkHistoryRepository,
    MetadataHistoryRepository,
    ProbeHistoryRepository,
    SyncHistoryRepository,
)
from ..storage.providers import Provider, ProviderRepository
from ..sync.engine import SyncEngine, SyncRequest
from ..utils.redact import redact
from ..utils.timeutil import now_iso, seconds_between
from .progress import JobProgress

log = logging.getLogger(__name__)

MAX_REMEMBERED_JOBS = 50


class BusyError(Exception):
    pass


class JobManager:
    def __init__(self, db: Database, settings_store: SettingsStore, paths: AppPaths,
                 client_factory: Optional[Callable] = None, probe_runner: Optional[Callable] = None,
                 scrapers: Optional["ScraperManager"] = None) -> None:
        self.db = db
        self.settings_store = settings_store
        self.paths = paths
        self.providers = ProviderRepository(db)
        self.sync_history = SyncHistoryRepository(db)
        self.probe_history = ProbeHistoryRepository(db)
        self.metadata_history = MetadataHistoryRepository(db)
        self.artwork_history = ArtworkHistoryRepository(db)
        self.scrapers = scrapers
        self.client_factory = client_factory  # tests inject fake Xtream clients
        self.probe_runner = probe_runner  # tests inject a fake ffprobe
        self._jobs: "OrderedDict[str, JobProgress]" = OrderedDict()
        self._provider_locks: dict[int, threading.Lock] = {}
        self._provider_activity: dict[int, str] = {}
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    # -- job registry ----------------------------------------------------------------------------
    def _register(self, progress: JobProgress) -> None:
        with self._lock:
            self._jobs[progress.id] = progress
            while len(self._jobs) > MAX_REMEMBERED_JOBS:
                oldest = next(iter(self._jobs))
                if self._jobs[oldest].active:
                    break
                self._jobs.pop(oldest)

    def jobs(self) -> list[dict]:
        with self._lock:
            items = list(self._jobs.values())
        return [j.to_dict() for j in reversed(items)]

    def get(self, job_id: str) -> Optional[JobProgress]:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or not job.active:
            return False
        job.cancel()
        return True

    def provider_activity(self) -> dict[int, str]:
        with self._lock:
            return dict(self._provider_activity)

    def _lock_for(self, provider_id: int) -> threading.Lock:
        with self._lock:
            return self._provider_locks.setdefault(provider_id, threading.Lock())

    def is_busy(self, provider_id: int) -> bool:
        return self._lock_for(provider_id).locked()

    @contextmanager
    def provider_locked(self, provider_id: int, activity: str) -> Iterator[None]:
        """Hold the provider's job lock for a short operation outside a job (``BusyError`` if taken)."""
        lock = self._lock_for(provider_id)
        if not lock.acquire(blocking=False):
            raise BusyError("A job is running for this provider; try again when it has finished")
        try:
            with self._lock:
                self._provider_activity[provider_id] = activity
            yield
        finally:
            with self._lock:
                self._provider_activity.pop(provider_id, None)
            lock.release()

    def wait_all(self, timeout: Optional[float] = None) -> None:
        for thread in list(self._threads):
            thread.join(timeout)

    def _spawn(self, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True, name=f"job-{args[0].id}")
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()

    def _targets(self, provider_id: Optional[int]) -> list[Provider]:
        if provider_id is not None:
            provider = self.providers.get(provider_id)
            if provider is None:
                raise KeyError(provider_id)
            if self.is_busy(provider_id):
                raise BusyError(f"A job is already running for provider '{provider.name}'")
            return [provider]
        return [p for p in self.providers.list() if p.enabled]

    # -- sync ----------------------------------------------------------------------------------------
    def start_sync(self, provider_id: Optional[int], request: SyncRequest) -> JobProgress:
        targets = self._targets(provider_id)
        scope = {"all": "Movies + Series", "movies": "Movies", "series": "Series"}[request.sync_type]
        who = targets[0].name if provider_id is not None else "all enabled providers"
        progress = JobProgress("sync", f"Sync {scope}: {who}")
        self._register(progress)
        self._spawn(self._run_sync, progress, [p.id for p in targets], request)
        return progress

    def _run_sync(self, progress: JobProgress, provider_ids: list[int], request: SyncRequest) -> None:
        progress.start()
        settings = self.settings_store.load()
        statuses: list[str] = []
        totals = {"created": 0, "updated": 0, "skipped": 0, "missing": 0, "errors": 0}
        try:
            all_providers = self.providers.list()
            folders = provider_folder_names(
                [(p.id, p.name, p.target_folder or settings.default_target_folder) for p in all_providers],
                settings.max_name_length,
            )
            if not provider_ids:
                progress.error("No enabled providers to synchronize")
            for pid in provider_ids:
                if progress.cancelled:
                    statuses.append("cancelled")
                    break
                provider = self.providers.get(pid)
                if provider is None:
                    continue
                lock = self._lock_for(pid)
                if not lock.acquire(blocking=False):
                    progress.error(f"{provider.name}: another job is running for this provider; skipped")
                    statuses.append("partial")
                    continue
                try:
                    with self._lock:
                        self._provider_activity[pid] = "syncing"
                    status, summary = self._sync_provider(progress, provider, folders[pid], settings, request)
                    statuses.append(status)
                    for key in totals:
                        totals[key] += summary.get(key, 0)
                finally:
                    with self._lock:
                        self._provider_activity.pop(pid, None)
                    lock.release()
            self._retention(settings)
        except Exception as exc:
            log.exception("Sync job crashed")
            progress.error(f"Sync job crashed: {exc}")
            statuses.append("failed")
        progress.finish(_overall(statuses), totals)

    def _sync_provider(self, progress: JobProgress, provider: Provider, folder: str, settings: Settings,
                       request: SyncRequest) -> tuple[str, dict]:
        started = now_iso()
        row = self.sync_history.start(progress.id, provider.id, provider.name, started, sync_type=request.sync_type)
        progress.history_ids.append(row)
        client = self.client_factory(provider, settings) if self.client_factory else None
        engine = SyncEngine(self.db, settings, provider, folder, progress, client=client)
        status, stats = engine.run(request)
        finished = now_iso()
        summary = stats.summary()
        self.sync_history.finish(
            row, status, finished, seconds_between(started, finished) or 0.0,
            {**stats.counters(), "categories": stats.categories}, stats.warnings, stats.errors, summary,
        )
        return status, {**summary, "errors": len(stats.errors)}

    # -- probe --------------------------------------------------------------------------------------
    def start_probe(self, provider_id: Optional[int], request: ProbeRequest) -> JobProgress:
        targets = self._targets(provider_id)
        scope = {"all": "Movies + Series", "movies": "Movies", "series": "Series"}[request.scope]
        who = targets[0].name if provider_id is not None else "all enabled providers"
        progress = JobProgress("probe", f"{'Force re-probe' if request.force else 'Probe'} {scope}: {who}")
        self._register(progress)
        self._spawn(self._run_probe, progress, [p.id for p in targets], request)
        return progress

    def ffprobe_location(self, settings: Optional[Settings] = None) -> tuple[Optional[str], str]:
        settings = settings or self.settings_store.load()
        return locate_ffprobe(settings.ffprobe_path, self.paths.root)

    def _run_probe(self, progress: JobProgress, provider_ids: list[int], request: ProbeRequest) -> None:
        progress.start()
        settings = self.settings_store.load()
        statuses: list[str] = []
        totals = {"considered": 0, "probed": 0, "skipped": 0, "succeeded": 0, "failed": 0}
        ffprobe, how = self.ffprobe_location(settings)
        if ffprobe is None and self.probe_runner is None:
            message = f"Cannot probe: {how}. Install FFmpeg or set the ffprobe path in Settings."
            progress.error(message)
            for pid in provider_ids:
                provider = self.providers.get(pid)
                if provider is not None:
                    started = now_iso()
                    row = self.probe_history.start(progress.id, pid, provider.name, started,
                                                   scope=request.scope, forced=int(request.force))
                    self.probe_history.finish(row, "failed", started, 0.0, {}, [], [message], {})
            progress.finish("failed", totals)
            return
        try:
            if not provider_ids:
                progress.error("No enabled providers to probe")
            for pid in provider_ids:
                if progress.cancelled:
                    statuses.append("cancelled")
                    break
                provider = self.providers.get(pid)
                if provider is None:
                    continue
                lock = self._lock_for(pid)
                if not lock.acquire(blocking=False):
                    progress.error(f"{provider.name}: another job is running for this provider; skipped")
                    statuses.append("partial")
                    continue
                try:
                    with self._lock:
                        self._provider_activity[pid] = "probing"
                    progress.update(provider=provider.name, processed=0, total=0)
                    started = now_iso()
                    row = self.probe_history.start(progress.id, pid, provider.name, started,
                                                   scope=request.scope, forced=int(request.force))
                    progress.history_ids.append(row)
                    kwargs = {"runner": self.probe_runner} if self.probe_runner else {}
                    engine = ProbeEngine(self.db, settings, ffprobe or "ffprobe", progress,
                                         user_agent=provider.user_agent, **kwargs)
                    status, stats = engine.run(
                        ProbeRequest(provider_id=pid, kinds=request.kinds, force=request.force), provider.name
                    )
                    finished = now_iso()
                    self.probe_history.finish(
                        row, status, finished, seconds_between(started, finished) or 0.0,
                        probe_summary_dict(stats), stats.warnings, stats.errors, stats.summary(),
                    )
                    statuses.append(status)
                    for key, value in stats.summary().items():
                        totals[key] += value
                finally:
                    with self._lock:
                        self._provider_activity.pop(pid, None)
                    lock.release()
            self._retention(settings)
        except Exception as exc:
            log.exception("Probe job crashed")
            progress.error(f"Probe job crashed: {redact(exc)}")
            statuses.append("failed")
        progress.finish(_overall(statuses), totals)

    # -- metadata scraping ----------------------------------------------------------------------------
    def start_scrape(self, provider_id: Optional[int], request: ScrapeRequest) -> JobProgress:
        if self.scrapers is None:
            raise RuntimeError("Metadata scraping is not available")
        targets = self._targets(provider_id)
        scope = {"all": "Movies + Series", "movies": "Movies", "series": "Series"}[request.scope]
        who = targets[0].name if provider_id is not None else "all enabled providers"
        progress = JobProgress("metadata", f"{'Force metadata refresh' if request.force else 'Scrape metadata'} {scope}: {who}")
        self._register(progress)
        self._spawn(self._run_scrape, progress, [p.id for p in targets], request)
        return progress

    def start_item_scrape(self, provider_id: int, item_kind: str, category_id: str, item_id: str,
                          title: str) -> JobProgress:
        """Scrape one movie or series (with its seasons and episodes) through the normal engine."""
        if self.scrapers is None:
            raise RuntimeError("Metadata scraping is not available")
        target = self._targets(provider_id)[0]
        request = ScrapeRequest(provider_id=provider_id, kinds=(item_kind,), force=True,
                                item=(item_kind, category_id, item_id))
        progress = JobProgress("metadata", f"Scrape metadata for '{title}': {target.name}")
        self._register(progress)
        self._spawn(self._run_scrape, progress, [target.id], request)
        return progress

    def _run_scrape(self, progress: JobProgress, provider_ids: list[int], request: ScrapeRequest) -> None:
        progress.start()
        settings = self.settings_store.load()
        statuses: list[str] = []
        totals = {"considered": 0, "matched": 0, "unmatched": 0, "ambiguous": 0, "updated": 0, "unchanged": 0,
                  "skipped": 0, "errors": 0}
        try:
            if not provider_ids:
                progress.error("No enabled providers to scrape")
            for pid in provider_ids:
                if progress.cancelled:
                    statuses.append("cancelled")
                    break
                provider = self.providers.get(pid)
                if provider is None:
                    continue
                lock = self._lock_for(pid)
                if not lock.acquire(blocking=False):
                    progress.error(f"{provider.name}: another job is running for this provider; skipped")
                    statuses.append("partial")
                    continue
                try:
                    with self._lock:
                        self._provider_activity[pid] = "scraping metadata"
                    progress.update(provider=provider.name, processed=0, total=0)
                    started = now_iso()
                    row = self.metadata_history.start(progress.id, pid, provider.name, started,
                                                      scope=request.scope, forced=int(request.force))
                    progress.history_ids.append(row)
                    root = effective_root(provider.target_folder, settings.default_target_folder)
                    engine = ScrapeEngine(self.db, settings, self.scrapers, progress, library_root=root)
                    status, stats = engine.run(dataclasses.replace(request, provider_id=pid), provider.name)
                    finished = now_iso()
                    self.metadata_history.set_plugins(row, stats.plugins_used)
                    self.metadata_history.finish(
                        row, status, finished, seconds_between(started, finished) or 0.0,
                        {**stats.counters(), "plugins": stats.plugins_used}, stats.warnings, stats.errors,
                        stats.summary(),
                    )
                    statuses.append(status)
                    for key, value in stats.summary().items():
                        totals[key] += value
                    totals["errors"] += stats.errors_count
                finally:
                    with self._lock:
                        self._provider_activity.pop(pid, None)
                    lock.release()
            self._retention(settings)
        except Exception as exc:
            log.exception("Metadata job crashed")
            progress.error(f"Metadata job crashed: {redact(exc)}")
            statuses.append("failed")
        progress.finish(_overall(statuses), totals)

    # -- artwork reconciliation ---------------------------------------------------------------------
    def start_artwork(self, provider_id: Optional[int], force: bool = False) -> JobProgress:
        """Reconcile existing artwork with the current artwork settings (every provider by default)."""
        if provider_id is not None:
            targets = self._targets(provider_id)
        else:
            targets = self.providers.list()
        mode = self.settings_store.load().artwork_mode
        who = targets[0].name if provider_id is not None else "all providers"
        progress = JobProgress("artwork", f"{'Force replace managed artwork' if force else 'Reconcile artwork'} "
                                          f"({mode}): {who}")
        self._register(progress)
        self._spawn(self._run_artwork, progress, [p.id for p in targets], force)
        return progress

    def _run_artwork(self, progress: JobProgress, provider_ids: list[int], force: bool) -> None:
        progress.start()
        settings = self.settings_store.load()
        statuses: list[str] = []
        totals = {"considered": 0, "downloaded": 0, "nfo_urls_written": 0, "local_removed": 0,
                  "nfo_refs_removed": 0, "unchanged": 0, "skipped": 0, "errors": 0}
        try:
            if not provider_ids:
                statuses.append("success")  # nothing to reconcile
            for pid in provider_ids:
                if progress.cancelled:
                    statuses.append("cancelled")
                    break
                provider = self.providers.get(pid)
                if provider is None:
                    continue
                lock = self._lock_for(pid)
                if not lock.acquire(blocking=False):
                    progress.error(f"{provider.name}: another job is running for this provider; skipped")
                    statuses.append("partial")
                    continue
                try:
                    with self._lock:
                        self._provider_activity[pid] = "reconciling artwork"
                    progress.update(provider=provider.name, processed=0, total=0)
                    started = now_iso()
                    row = self.artwork_history.start(progress.id, pid, provider.name, started,
                                                     mode=settings.artwork_mode, forced=int(force))
                    progress.history_ids.append(row)
                    root = effective_root(provider.target_folder, settings.default_target_folder)
                    engine = ArtworkReconciler(self.db, settings, progress, root)
                    status, stats = engine.run(pid, provider.name, force=force)
                    finished = now_iso()
                    self.artwork_history.finish(
                        row, status, finished, seconds_between(started, finished) or 0.0,
                        stats.counters(), stats.warnings, stats.errors, stats.summary(),
                    )
                    statuses.append(status)
                    for key, value in stats.summary().items():
                        totals[key] += value
                    totals["errors"] += stats.errors_count
                finally:
                    with self._lock:
                        self._provider_activity.pop(pid, None)
                    lock.release()
            self._retention(settings)
        except Exception as exc:
            log.exception("Artwork job crashed")
            progress.error(f"Artwork job crashed: {redact(exc)}")
            statuses.append("failed")
        progress.finish(_overall(statuses), totals)

    def _retention(self, settings: Settings) -> None:
        try:
            self.sync_history.apply_retention(settings.history_keep_per_provider, settings.history_max_age_days)
            self.probe_history.apply_retention(settings.history_keep_per_provider, settings.history_max_age_days)
            self.metadata_history.apply_retention(settings.history_keep_per_provider, settings.history_max_age_days)
            self.artwork_history.apply_retention(settings.history_keep_per_provider, settings.history_max_age_days)
        except Exception:
            log.exception("History retention failed")


def _overall(statuses: list[str]) -> str:
    if not statuses:
        return "failed"
    if "cancelled" in statuses:
        return "cancelled"
    if all(s == "success" for s in statuses):
        return "success"
    if all(s == "failed" for s in statuses):
        return "failed"
    return "partial"


KIND_SCOPES = {"all": (MOVIE, EPISODE), "movies": (MOVIE,), "series": (EPISODE,)}
