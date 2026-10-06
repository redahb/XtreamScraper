"""Library-wide figures for the WebUI top bar (local data only, never a network call)."""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional, TypedDict

from .db import Database
from .scrapers import ScraperStateRepository


def database_size(db_path: str) -> int:
    """Live footprint of the SQLite database: the main file plus its WAL (the SHM file is transient).

    Contract:
        Silences:
            - ``OSError`` per file (missing, no access): that file counts as 0 bytes, so an
              unreadable database reports 0 rather than failing.
    """
    size = 0
    for path in (db_path, db_path + "-wal"):
        try:
            size += os.path.getsize(path)
        except OSError:
            pass
    return size


SUMMARY_TTL_SECONDS = 10.0

#: ``(normalised database path, sorted enabled plugin IDs)``: what the counted figures depend on.
_Key = tuple[str, tuple[str, ...]]


class CountedFigures(TypedDict):
    """The expensive part of the library summary, kept by :class:`SummaryCache`."""

    providers: int
    database_items: int
    matched_titles: int
    total_titles: int


class SummaryCache:
    """Keeps the expensive library figures for a short time.

    The WebUI polls ``/api/status`` every 2 s during jobs; recounting every episode and the
    match summary on each poll is wasted work. Figures may lag by at most ``ttl`` seconds;
    :meth:`invalidate` (e.g. when a job finishes) makes the next call recompute them.
    """

    def __init__(self, ttl: float = SUMMARY_TTL_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self.clock = clock
        self._lock = threading.Lock()
        self._entry: Optional[tuple[_Key, float, CountedFigures]] = None

    def get(self, key: _Key, compute: Callable[[], CountedFigures]) -> CountedFigures:
        """The cached figures for ``key`` while fresh, else ``compute()`` (stored for ``ttl`` s).

        One entry only: a different key recomputes and replaces it. ``compute`` runs under the
        lock, so concurrent callers wait instead of counting twice; its exceptions propagate
        and nothing is cached. Callers get a copy of the cached dict.
        """
        with self._lock:
            now = self.clock()
            if self._entry is not None:
                cached_key, expires, value = self._entry
                if cached_key == key and now < expires:
                    return value.copy()
            value = compute()
            self._entry = (key, now + self.ttl, value.copy())
            return value

    def invalidate(self) -> None:
        with self._lock:
            self._entry = None


#: Shared by the WebUI status endpoint; call ``invalidate_summary()`` after data changes.
summary_cache = SummaryCache()


def invalidate_summary() -> None:
    """Make the next ``library_summary`` call recompute its figures."""
    summary_cache.invalidate()


def _counted_figures(db: Database, enabled_plugin_ids: list[str]) -> CountedFigures:
    row = db.query_one(
        "SELECT (SELECT COUNT(*) FROM providers) AS providers, "
        "(SELECT COUNT(*) FROM movies) + (SELECT COUNT(*) FROM series) + (SELECT COUNT(*) FROM episodes) AS items")
    assert row is not None  # an aggregate query always returns one row
    matched, total = ScraperStateRepository(db).root_match_summary(enabled_plugin_ids)
    return {
        "providers": int(row["providers"]),
        "database_items": int(row["items"]),
        "matched_titles": matched,
        "total_titles": total,
    }


def library_summary(db: Database, db_path: str, installed_plugins: int, enabled_plugin_ids: list[str],
                    cache: Optional[SummaryCache] = None) -> dict[str, int]:
    """Providers, plugins, persisted media records (movies, series and episodes in any lifecycle
    state), database size, and overall matched titles out of active movies + series.

    The counted figures come from ``cache`` (default: the shared :data:`summary_cache`).
    """
    cache = summary_cache if cache is None else cache
    key: _Key = (os.path.normcase(os.path.abspath(db_path)), tuple(sorted(enabled_plugin_ids)))
    figures = cache.get(key, lambda: _counted_figures(db, enabled_plugin_ids))
    return {
        "providers": figures["providers"],
        "plugins": installed_plugins,
        "database_items": figures["database_items"],
        "database_size_bytes": database_size(db_path),
        "matched_titles": figures["matched_titles"],
        "total_titles": figures["total_titles"],
    }
