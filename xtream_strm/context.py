"""Wiring of the application's long-lived services."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from .config.paths import AppPaths
from .config.settings import SettingsStore
from .jobs.manager import JobManager
from .metadata.registry import registry
from .storage.categories import CategoryRepository
from .storage.db import Database
from .storage.history import ProbeHistoryRepository, SyncHistoryRepository
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
    log_path: str = ""
    # Factory for Xtream clients: (provider, settings, **overrides) -> client. Tests replace it.
    client_factory: Optional[Callable] = None
    extra: dict = field(default_factory=dict)


def build_context(paths: AppPaths, log_path: str = "", client_factory: Optional[Callable] = None,
                  probe_runner: Optional[Callable] = None) -> AppContext:
    db = Database(paths.db_path)
    db.migrate()
    settings_store = SettingsStore(db)
    providers = ProviderRepository(db)
    providers.register_all_secrets()
    sync_history = SyncHistoryRepository(db)
    probe_history = ProbeHistoryRepository(db)
    interrupted = sync_history.mark_interrupted() + probe_history.mark_interrupted()
    jobs = JobManager(db, settings_store, paths, client_factory=client_factory, probe_runner=probe_runner)
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
    )
    ctx.extra["interrupted_jobs"] = interrupted
    # Optional third-party metadata providers: <app>/plugins/*.py
    registry.discover_plugins(os.path.join(paths.root, "plugins"))
    return ctx
