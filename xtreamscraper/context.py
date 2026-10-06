"""Wiring of the application's long-lived services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .config.paths import AppPaths
from .config.settings import SettingsStore
from .jobs.manager import ClientFactory, JobManager
from .metadata.manager import ScraperManager
from .metadata.manual_matching import ManualMatchService
from .metadata.registry import PluginRegistry
from .probe.engine import Runner
from .storage.artwork import ArtworkRepository
from .storage.categories import CategoryRepository
from .storage.db import Database
from .storage.history import MetadataHistoryRepository, ProbeHistoryRepository, SyncHistoryRepository
from .storage.probe_state import ProbeStateRepository
from .storage.providers import ProviderRepository
from .storage.sync_state import SyncStateRepository


@dataclass
class AppContext:
    paths: AppPaths
    db: Database
    settings_store: SettingsStore
    providers: ProviderRepository
    categories: CategoryRepository
    sync_state: SyncStateRepository
    probe_state: ProbeStateRepository
    sync_history: SyncHistoryRepository
    probe_history: ProbeHistoryRepository
    jobs: JobManager
    scrapers: ScraperManager
    metadata_history: MetadataHistoryRepository
    manual_matching: ManualMatchService
    artwork: ArtworkRepository
    log_path: str = ""
    # Factory for Xtream clients: (provider, settings) -> client. Tests replace it.
    client_factory: Optional[ClientFactory] = None
    # Runtime extras: "shutdown" (callback set by app.py), "shutting_down" (set by POST /api/shutdown),
    # "interrupted_jobs" (jobs marked interrupted at start-up).
    extra: dict[str, Any] = field(default_factory=dict)


def build_context(paths: AppPaths, log_path: str = "", client_factory: Optional[ClientFactory] = None,
                  probe_runner: Optional[Runner] = None, registry: Optional[PluginRegistry] = None) -> AppContext:
    """Open and migrate the database and build every long-lived service.

    Contract:
        Preconditions:
            - Call it once per data dir and process: it marks every history row still
              ``running`` as ``interrupted`` (the count is in ``extra["interrupted_jobs"]``), which
              would also hit jobs of another live context on the same DB.
            - Stored provider credentials are registered with ``redact`` right after the
              migration and plugin secrets when ``ScraperManager`` is built; nothing that may log
              provider or plugin data must run before those two steps.
        Raises:
            sqlite3.Error, OSError: Opening or migrating the database (not caught).
        Silences:
            - With ``registry=None`` plugins are discovered in ``paths.plugins_dir``; a missing
              folder or a plugin that fails to load is skipped (logged by the registry).
    """
    db = Database(paths.db_path)
    db.migrate()
    settings_store = SettingsStore(db)
    providers = ProviderRepository(db)
    providers.register_all_secrets()
    sync_history = SyncHistoryRepository(db)
    probe_history = ProbeHistoryRepository(db)
    metadata_history = MetadataHistoryRepository(db)
    from .storage.history import ArtworkHistoryRepository

    interrupted = (sync_history.mark_interrupted() + probe_history.mark_interrupted()
                   + metadata_history.mark_interrupted() + ArtworkHistoryRepository(db).mark_interrupted())
    # Scraper plugins: nothing is built in; whatever is installed in <app>/plugins/ is loaded.
    if registry is None:
        registry = PluginRegistry()
        registry.discover(paths.plugins_dir)
    scrapers = ScraperManager(db, registry)
    jobs = JobManager(db, settings_store, paths, client_factory=client_factory, probe_runner=probe_runner,
                      scrapers=scrapers)
    ctx = AppContext(
        paths=paths,
        db=db,
        settings_store=settings_store,
        providers=providers,
        categories=CategoryRepository(db),
        sync_state=SyncStateRepository(db),
        probe_state=ProbeStateRepository(db),
        sync_history=sync_history,
        probe_history=probe_history,
        jobs=jobs,
        log_path=log_path,
        client_factory=client_factory,
        scrapers=scrapers,
        metadata_history=metadata_history,
        manual_matching=ManualMatchService(db, scrapers, jobs),
        artwork=ArtworkRepository(db),
    )
    ctx.extra["interrupted_jobs"] = interrupted
    return ctx
