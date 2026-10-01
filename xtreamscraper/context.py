"""Wiring of the application's long-lived services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .config.paths import AppPaths
from .config.settings import SettingsStore
from .jobs.manager import JobManager
from .metadata.manager import ScraperManager
from .metadata.registry import PluginRegistry
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
    scrapers: Optional[ScraperManager] = None
    metadata_history: Optional[MetadataHistoryRepository] = None
    log_path: str = ""
    # Factory for Xtream clients: (provider, settings, **overrides) -> client. Tests replace it.
    client_factory: Optional[Callable] = None
    extra: dict = field(default_factory=dict)


def build_context(paths: AppPaths, log_path: str = "", client_factory: Optional[Callable] = None,
                  probe_runner: Optional[Callable] = None, registry: Optional[PluginRegistry] = None) -> AppContext:
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
    )
    ctx.extra["interrupted_jobs"] = interrupted
    return ctx
