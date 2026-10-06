"""Background sync/probe/metadata/artwork jobs.

Each job runs in its own thread so the web server stays responsive. A per-provider lock
guarantees that one provider is never synchronized (or probed) by two jobs at once;
different providers may be processed concurrently.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional

from ..artwork.reconcile import ArtworkReconciler, effective_root
from ..config.paths import AppPaths
from ..config.settings import Settings, SettingsStore
from ..filesystem.layout import provider_folder_names
from ..library.models import EPISODE, MOVIE
from ..probe.engine import ProbeEngine, ProbeRequest, Runner, probe_summary_dict
from ..probe.ffprobe_runner import locate_ffprobe
from ..storage.db import Database
from ..storage.summary import invalidate_summary
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
from ..xtream.client import XtreamClient
from .progress import JobCancelled, JobProgress

if TYPE_CHECKING:
    from ..storage.history import _HistoryBase

log = logging.getLogger(__name__)

MAX_REMEMBERED_JOBS = 50

# Builds the Xtream client a sync uses; tests inject one that returns a fake client.
ClientFactory = Callable[[Provider, Settings], XtreamClient]
# What a job's ``work(row)`` returns to ``_recorded``: status, counters, warnings, errors, summary.
WorkResult = tuple[str, dict[str, Any], list[str], list[str], dict[str, int]]


class BusyError(Exception):
    pass


class UnknownProviderError(KeyError):
    """The requested provider does not exist (a ``KeyError`` for older callers)."""


class JobManager:
    def __init__(self, db: Database, settings_store: SettingsStore, paths: AppPaths,
                 client_factory: Optional[ClientFactory] = None, probe_runner: Optional[Runner] = None,
                 scrapers: Optional[ScraperManager] = None) -> None:
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
        self._jobs: OrderedDict[str, JobProgress] = OrderedDict()
        self._provider_locks: dict[int, threading.Lock] = {}
        self._provider_activity: dict[int, str] = {}
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    # -- job registry ----------------------------------------------------------------------------
    def _register(self, progress: JobProgress) -> None:
        """Remember a new job and forget the oldest finished ones beyond ``MAX_REMEMBERED_JOBS``.

        Contract:
            Silences:
                - Eviction stops at the oldest job that is still active, so while an old job runs
                  the registry can hold more than ``MAX_REMEMBERED_JOBS`` entries (finished jobs
                  behind it are kept too). Nothing is evicted out of order.
        """
        with self._lock:
            self._jobs[progress.id] = progress
            while len(self._jobs) > MAX_REMEMBERED_JOBS:
                oldest = next(iter(self._jobs))
                if self._jobs[oldest].active:
                    break
                self._jobs.pop(oldest)

    def jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._jobs.values())
        return [j.to_dict() for j in reversed(items)]

    def get(self, job_id: str) -> Optional[JobProgress]:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        """Ask one job to stop. ``False`` for an unknown or already finished job.

        Contract:
            Silences:
                - ``True`` means only that the cancel was requested: the job stops at its next
                  ``check_cancelled``; one already past its last check finishes with its own status.
        """
        job = self._jobs.get(job_id)
        if job is None or not job.active:
            return False
        job.cancel()
        return True

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.active)

    def cancel_all(self) -> int:
        """Ask every running job to stop; returns how many were asked."""
        with self._lock:
            active = [j for j in self._jobs.values() if j.active]
        for job in active:
            job.cancel()
        return len(active)

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
        """Hold the provider's job lock for a short operation outside a job (``BusyError`` if taken).

        Contract:
            Preconditions:
                - Never nest it for the same provider in one thread, and never start a job for
                  that provider from inside it: the lock is a plain non-reentrant
                  ``threading.Lock``, so the inner acquire raises ``BusyError``.
            Raises:
                BusyError: The lock is already held (by a job or another short operation);
                    explicit, non-blocking.
        """
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
        """Wait for the job threads to end, at most ``timeout`` seconds in total (``None``: forever).

        Contract:
            Silences:
                - Returns without any signal when the deadline passes with threads still running;
                  callers that care check ``active_count()`` afterwards. A thread started after
                  the call began is not waited for.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in list(self._threads):
            thread.join(None if deadline is None else max(0.0, deadline - time.monotonic()))

    def _spawn(self, target: Callable[..., None], *args: Any) -> None:
        """Run ``target(*args)`` in a daemon job thread; ``args[0]`` must be the job's progress.

        Contract:
            Preconditions:
                - ``args[0]`` is the ``JobProgress`` of the job (``IndexError`` /
                  ``AttributeError`` at once otherwise, before any thread starts).
            Silences:
                - Any ``Exception`` escaping ``target`` (outside the per-provider handling of
                  ``_run_providers``) is logged and recorded as a redacted progress error; a
                  ``JobCancelled`` escaping it is dropped. In both cases a job still active
                  afterwards is finished as ``cancelled`` (cancel requested) or ``failed``.
                - A failure closing the thread's DB connection is logged only.
        """
        progress: JobProgress = args[0]

        def run() -> None:
            try:
                target(*args)
            except JobCancelled:
                pass  # the finally below records it
            except Exception as exc:  # thread boundary: a crash outside the per-provider handling
                log.exception("%s job crashed", progress.kind)
                progress.error(f"Job crashed: {redact(exc)}")
            finally:
                if progress.active:  # never leave a job "running" after its thread has ended
                    progress.finish("cancelled" if progress.cancelled else "failed")
                invalidate_summary()  # the top-bar figures change with every job
                try:
                    self.db.release_thread()  # job threads are short-lived: close their connection
                except Exception:
                    log.exception("Closing the job's database connection failed")

        thread = threading.Thread(target=run, daemon=True, name=f"job-{progress.id}")
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()

    def _targets(self, provider_id: Optional[int]) -> list[Provider]:
        """The providers a new job runs for: the one given, or every enabled provider (``None``).

        Contract:
            Raises:
                UnknownProviderError: ``provider_id`` names no stored provider (explicit).
                BusyError: ``provider_id``'s job lock is held right now (explicit). The check is
                    advisory: a job started a moment later can still take the lock first; the
                    job then skips that provider as ``partial`` in ``_run_providers``.
            Silences:
                - A given provider is used even when it is disabled; with ``None`` disabled
                  providers are left out and busy ones are not checked here (each is skipped
                  later if busy). An empty list is returned when no provider is enabled.
        """
        if provider_id is not None:
            provider = self.providers.get(provider_id)
            if provider is None:
                raise UnknownProviderError(provider_id)
            if self.is_busy(provider_id):
                raise BusyError(f"A job is already running for provider '{provider.name}'")
            return [provider]
        return [p for p in self.providers.list() if p.enabled]

    # -- sync ----------------------------------------------------------------------------------------
    def start_sync(self, provider_id: Optional[int], request: SyncRequest) -> JobProgress:
        """Start a sync job for one provider or every enabled provider and return its progress.

        Contract:
            Raises:
                UnknownProviderError, BusyError: From ``_targets`` (no job is registered).
        """
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
        totals = {"created": 0, "updated": 0, "skipped": 0, "missing": 0, "errors": 0}
        folders: dict[int, str] = {}

        def run_one(provider: Provider) -> str:
            if not folders:
                folders.update(provider_folder_names(
                    [(p.id, p.name, p.target_folder or settings.default_target_folder) for p in self.providers.list()],
                    settings.max_name_length,
                ))
            status, summary = self._sync_provider(progress, provider, folders[provider.id], settings, request)
            for key in totals:
                totals[key] += summary.get(key, 0)
            return status

        self._run_providers(progress, provider_ids, "syncing", "Sync", totals, settings, run_one,
                            no_providers="No enabled providers to synchronize")

    def _sync_provider(self, progress: JobProgress, provider: Provider, folder: str, settings: Settings,
                       request: SyncRequest) -> tuple[str, dict[str, int]]:
        client = self.client_factory(provider, settings) if self.client_factory else None
        result: dict[str, int] = {}

        def work(row: int) -> WorkResult:
            engine = SyncEngine(self.db, settings, provider, folder, progress, client=client)
            status, stats = engine.run(request)
            summary = stats.summary()
            result.update(summary, errors=stats.errors_count)
            return (status, {**stats.counters(), "categories": stats.categories}, stats.warnings, stats.errors,
                    summary)

        status = self._recorded(self.sync_history, progress, provider, {"sync_type": request.sync_type}, work)
        return status, result

    # -- shared job plumbing ---------------------------------------------------------------------------
    def _run_providers(self, progress: JobProgress, provider_ids: list[int], activity: str, label: str,
                       totals: dict[str, int], settings: Settings, run_one: Callable[[Provider], str],
                       no_providers: Optional[str] = None) -> None:
        """Run ``run_one`` for every provider under its job lock, apply history retention and
        finish the job. ``no_providers``: the error for an empty list (``None``: success).

        Contract:
            Preconditions:
                - ``run_one`` returns one of ``success``/``partial``/``failed``/``cancelled`` and
                  adds its figures to ``totals`` itself; ``totals`` becomes the job summary.
                - The lock acquire must stay non-blocking: a job waiting on a plain
                  ``threading.Lock`` could not be cancelled.
            Silences:
                - A provider deleted after the job started is skipped without a message or a
                  status (a one-provider job then ends ``failed`` with no error).
                - A provider whose lock is taken is skipped with a progress error (``partial``).
                - Any ``Exception`` (from ``run_one``, the DB or retention) is logged, recorded
                  as a redacted progress error and ends the job ``failed`` or ``partial``; the
                  providers after it are not run. ``JobCancelled`` is not caught here (it
                  reaches ``_spawn``).
        """
        statuses: list[str] = []
        try:
            if not provider_ids:
                if no_providers:
                    progress.error(no_providers)
                else:
                    statuses.append("success")  # nothing to do
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
                        self._provider_activity[pid] = activity
                    statuses.append(run_one(provider))
                finally:
                    with self._lock:
                        self._provider_activity.pop(pid, None)
                    lock.release()
            self._retention(settings)
        except Exception as exc:
            log.exception("%s job crashed", label)
            progress.error(f"{label} job crashed: {redact(exc)}")
            statuses.append("failed")
        progress.finish(_overall(statuses), totals)

    def _recorded(self, history: _HistoryBase, progress: JobProgress, provider: Provider,
                  start_values: dict[str, Any], work: Callable[[int], WorkResult]) -> str:
        """Run ``work(row)`` -> ``(status, counters, warnings, errors, summary)`` inside a history
        row. A crash finishes the row as failed (instead of leaving it "running") and re-raises.

        Contract:
            Raises:
                Exception: Whatever ``work`` raised, after the row was finished as ``failed``;
                    DB errors from ``history.start`` / ``history.finish`` propagate.
            Silences:
                - An error while recording the crashed row is logged only, so the original
                  exception is the one re-raised (the row may then stay ``running`` until the
                  next start marks it interrupted).
                - A ``BaseException`` from ``work`` (``JobCancelled``) is not handled: the row is
                  left ``running``. The engines catch ``JobCancelled`` themselves.
        """
        started = now_iso()
        row = history.start(progress.id, provider.id, provider.name, started, **start_values)
        progress.history_ids.append(row)
        try:
            status, counters, warnings, errors, summary = work(row)
        except Exception as exc:
            finished = now_iso()
            try:
                history.finish(row, "failed", finished, seconds_between(started, finished) or 0.0, {}, [],
                               [redact(f"Job crashed: {exc}")], {})
            except Exception:
                log.exception("Recording the crashed job failed")
            raise
        finished = now_iso()
        history.finish(row, status, finished, seconds_between(started, finished) or 0.0, counters, warnings,
                       errors, summary)
        return status

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
        """Job body of a probe job.

        Contract:
            Silences:
                - Without an ffprobe (and no injected runner) the job fails at once with one
                  progress error, plus one ``failed`` history row per provider that still
                  exists; deleted providers are skipped silently.
        """
        progress.start()
        settings = self.settings_store.load()
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

        def run_one(provider: Provider) -> str:
            progress.update(provider=provider.name, processed=0, total=0)

            def work(row: int) -> WorkResult:
                kwargs = {"runner": self.probe_runner} if self.probe_runner else {}
                engine = ProbeEngine(self.db, settings, ffprobe or "ffprobe", progress,
                                     user_agent=provider.user_agent, **kwargs)
                status, stats = engine.run(
                    ProbeRequest(provider_id=provider.id, kinds=request.kinds, force=request.force), provider.name
                )
                for key, value in stats.summary().items():
                    totals[key] += value
                return status, probe_summary_dict(stats), stats.warnings, stats.errors, stats.summary()

            return self._recorded(self.probe_history, progress, provider,
                                  {"scope": request.scope, "forced": int(request.force)}, work)

        self._run_providers(progress, provider_ids, "probing", "Probe", totals, settings, run_one,
                            no_providers="No enabled providers to probe")

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
        """Scrape one movie or series (with its seasons and episodes) through the normal engine.

        Contract:
            Raises:
                RuntimeError: No scraper manager was given (explicit).
                UnknownProviderError, BusyError: From ``_targets``. A disabled provider is
                    accepted.
            Silences:
                - The item itself is not checked here; the engine handles an unknown item.
        """
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
        totals = {"considered": 0, "matched": 0, "unmatched": 0, "ambiguous": 0, "updated": 0, "unchanged": 0,
                  "skipped": 0, "errors": 0}
        def run_one(provider: Provider) -> str:
            progress.update(provider=provider.name, processed=0, total=0)

            def work(row: int) -> WorkResult:
                root = effective_root(provider.target_folder, settings.default_target_folder)
                engine = ScrapeEngine(self.db, settings, self.scrapers, progress, library_root=root)
                status, stats = engine.run(dataclasses.replace(request, provider_id=provider.id), provider.name)
                self.metadata_history.set_plugins(row, stats.plugins_used)
                for key, value in stats.summary().items():
                    totals[key] += value
                totals["errors"] += stats.errors_count
                return (status, {**stats.counters(), "plugins": stats.plugins_used}, stats.warnings, stats.errors,
                        stats.summary())

            return self._recorded(self.metadata_history, progress, provider,
                                  {"scope": request.scope, "forced": int(request.force)}, work)

        self._run_providers(progress, provider_ids, "scraping metadata", "Metadata", totals, settings, run_one,
                            no_providers="No enabled providers to scrape")

    # -- artwork reconciliation ---------------------------------------------------------------------
    def start_artwork(self, provider_id: Optional[int], force: bool = False) -> JobProgress:
        """Reconcile existing artwork with the current artwork settings (every provider by default).

        Contract:
            Raises:
                UnknownProviderError, BusyError: From ``_targets``, only for a given provider.
            Silences:
                - With ``provider_id=None`` disabled providers are included (unlike sync, probe
                  and scrape), and an empty provider list finishes ``success``.
        """
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
        totals = {"considered": 0, "downloaded": 0, "nfo_urls_written": 0, "local_removed": 0,
                  "nfo_refs_removed": 0, "unchanged": 0, "skipped": 0, "errors": 0}
        def run_one(provider: Provider) -> str:
            progress.update(provider=provider.name, processed=0, total=0)

            def work(row: int) -> WorkResult:
                root = effective_root(provider.target_folder, settings.default_target_folder)
                engine = ArtworkReconciler(self.db, settings, progress, root)
                status, stats = engine.run(provider.id, provider.name, force=force)
                for key, value in stats.summary().items():
                    totals[key] += value
                totals["errors"] += stats.errors_count
                return status, stats.counters(), stats.warnings, stats.errors, stats.summary()

            return self._recorded(self.artwork_history, progress, provider,
                                  {"mode": settings.artwork_mode, "forced": int(force)}, work)

        self._run_providers(progress, provider_ids, "reconciling artwork", "Artwork", totals, settings, run_one)

    def _retention(self, settings: Settings) -> None:
        """Apply the history retention settings to all four history tables.

        Contract:
            Silences:
                - Any ``Exception`` is logged only; the tables after the failing one are not
                  trimmed this time. The job status is unaffected.
        """
        try:
            self.sync_history.apply_retention(settings.history_keep_per_provider, settings.history_max_age_days)
            self.probe_history.apply_retention(settings.history_keep_per_provider, settings.history_max_age_days)
            self.metadata_history.apply_retention(settings.history_keep_per_provider, settings.history_max_age_days)
            self.artwork_history.apply_retention(settings.history_keep_per_provider, settings.history_max_age_days)
        except Exception:
            log.exception("History retention failed")


def _overall(statuses: list[str]) -> str:
    """The job status from its per-provider statuses.

    Contract:
        Silences:
            - An empty list means nothing ran: ``failed``. ``cancelled`` anywhere wins over
              every other status; any mix of other statuses is ``partial``.
    """
    if not statuses:
        return "failed"
    if "cancelled" in statuses:
        return "cancelled"
    if all(s == "success" for s in statuses):
        return "success"
    if all(s == "failed" for s in statuses):
        return "failed"
    return "partial"


KIND_SCOPES: dict[str, tuple[str, ...]] = {"all": (MOVIE, EPISODE), "movies": (MOVIE,), "series": (EPISODE,)}
