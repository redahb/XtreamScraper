"""Incremental Xtream -> STRM synchronization for one provider.

For every selected category the engine fetches the provider's current catalog,
compares it with the stored synchronization state (keyed by stable Xtream IDs) and writes
only what is new or changed.

Items a successful provider answer no longer lists become ``missing``: their ``.strm`` is
quarantined (renamed to ``.strm.bak``), everything else stays, and they are restored when
they come back. Absence is judged per content type only after every selected category was
processed, never from a failed request, never from a category the provider no longer lists,
and an empty answer for a category that had items only counts once a second consecutive
sync confirms it. Permanent removal of what the application owns is opt-in
(``missing_purge_days``) and needs such a confirmation too; see :mod:`.lifecycle`.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Iterable, Optional, TypeVar

from ..config.settings import Settings
from ..filesystem import atomic
from ..filesystem.layout import (
    LibraryLayout,
    NameAllocator,
    dedupe_candidates,
    id_suffix,
    rename_sidecars,
    with_suffix,
)
from ..filesystem.naming import (
    NameRules,
    clean_title,
    episode_base_name,
    movie_base_name,
    resolve_year,
    season_folder_name,
    series_base_name,
    series_display_title,
    version_label,
)
from ..filesystem.sanitize import sanitize_component
from ..filesystem.strm import WriteResult, read_strm_url, write_strm
from ..jobs.progress import JobCancelled, JobProgress
from ..storage.categories import MOVIE, SERIES, CategoryRecord, CategoryRepository
from ..storage.db import Database
from ..storage.providers import Provider
from ..storage.nfo_files import NfoFileRepository
from ..storage.sync_state import (
    ACTIVE,
    EpisodeRecord,
    MovieRecord,
    SeriesRecord,
    SyncStateRepository,
)
from ..utils.hashing import fingerprint, url_fingerprint
from ..utils.redact import redact
from ..utils.timeutil import now_iso, older_than
from ..xtream.client import ClientOptions, XtreamClient, XtreamError
from ..xtream.models import Episode, SeriesEntry, VodStream
from ..xtream.urls import build_episode_url, build_movie_url, stream_id_from_url
from ..nfo.paths import episode_nfo_path
from .lifecycle import MissingItemLifecycle
from .stats import SyncStats

log = logging.getLogger(__name__)

T = TypeVar("T")
BATCH_SIZE = 200


class FatalSyncError(Exception):
    """Stops the provider's sync (bad credentials, no target folder...)."""


@dataclass
class SyncRequest:
    movies: bool = True
    series: bool = True
    full_refresh: bool = False  # ignore the "series unchanged" shortcut

    @property
    def sync_type(self) -> str:
        if self.movies and self.series:
            return "all"
        return "movies" if self.movies else "series"


def id_sort_key(value: str) -> tuple[int, int, str]:
    return (0, int(value), "") if value.isdigit() else (1, 0, value)


def _unique_by(items: Iterable[T], key: Callable[[T], str]) -> list[T]:
    seen: set[str] = set()
    result: list[T] = []
    for item in items:
        k = key(item)
        if k not in seen:
            seen.add(k)
            result.append(item)
    return sorted(result, key=lambda i: id_sort_key(key(i)))


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def client_for(provider: Provider, settings: Settings) -> XtreamClient:
    return XtreamClient(
        provider.base_url,
        provider.username,
        provider.password,
        provider.user_agent,
        ClientOptions(
            connect_timeout=settings.http_connect_timeout,
            read_timeout=settings.http_read_timeout,
            retries=settings.http_retries,
            request_delay_ms=settings.request_delay_ms,
        ),
    )


class SyncEngine:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        provider: Provider,
        provider_folder: str,
        progress: JobProgress,
        client: Optional[XtreamClient] = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.provider = provider
        self.progress = progress
        self.state = SyncStateRepository(db)
        self.categories = CategoryRepository(db)
        self.rules = NameRules(
            max_length=settings.max_name_length,
            clean_titles=settings.clean_titles,
            episode_title_in_filename=settings.episode_title_in_filename,
        )
        self.target_root = provider.target_folder or settings.default_target_folder
        self.layout = LibraryLayout(self.target_root or ".", provider_folder)
        self._client = client
        self._owns_client = client is None
        self.stats = SyncStats()
        self.now = now_iso()
        self.nfo_files = NfoFileRepository(db)
        self.lifecycle = MissingItemLifecycle(db, settings, self.layout.provider_dir,
                                              os.path.abspath(self.target_root) if self.target_root else "", self.stats)
        # Changing the URL or credentials changes every stream URL.
        self.credentials_fp = fingerprint(provider.base_url, provider.username, provider.password)

    @property
    def client(self) -> XtreamClient:
        if self._client is None:
            self._client = client_for(self.provider, self.settings)
        return self._client

    # -- entry point ---------------------------------------------------------------------------
    def run(self, request: SyncRequest) -> tuple[str, SyncStats]:
        status = "success"
        log.info("Sync started: provider '%s' (%s)", self.provider.name, request.sync_type)
        try:
            self._run(request)
        except JobCancelled:
            status = "cancelled"
            self.stats.warn("Synchronization cancelled by user")
        except FatalSyncError as exc:
            status = "failed"
            self.stats.error(str(exc))
            log.error("Sync failed for provider '%s': %s", self.provider.name, redact(exc))
        except Exception as exc:  # never let one provider crash the job runner
            status = "failed"
            self.stats.error(f"Unexpected error: {redact(exc)}")
            log.exception("Unexpected sync error for provider '%s'", self.provider.name)
        finally:
            if self._owns_client and self._client is not None:
                self._client.close()
        if status == "success" and self.stats.errors:
            status = "partial"
        s = self.stats
        log.info(
            "Sync %s: provider '%s' – movies %d new/%d updated/%d unchanged/%d missing; series %d processed/%d unchanged; "
            "episodes %d new/%d updated/%d unchanged/%d missing; %d errors",
            status, self.provider.name, s.movies_created, s.movies_updated, s.movies_unchanged, s.movies_missing,
            s.series_processed, s.series_unchanged, s.episodes_created, s.episodes_updated, s.episodes_unchanged,
            s.episodes_missing, len(s.errors),
        )
        return status, self.stats

    def _run(self, request: SyncRequest) -> None:
        if not self.target_root:
            raise FatalSyncError("No target folder configured (set one on the provider or in Settings)")
        try:
            atomic.ensure_dir(self.layout.provider_dir)
        except OSError as exc:
            raise FatalSyncError(f"Cannot create library folder {self.layout.provider_dir}: {exc}") from None

        self.progress.update(provider=self.provider.name, phase="Connecting", category=None)
        try:
            account = self.client.authenticate()
        except XtreamError as exc:
            raise FatalSyncError(f"Cannot reach provider: {exc}") from None
        if not account.authenticated:
            raise FatalSyncError(f"Provider login failed: {account.summary()}")

        do_movies = request.movies and self.provider.movies_enabled
        do_series = request.series and self.provider.series_enabled
        if request.movies and not self.provider.movies_enabled:
            self.stats.warn("Movie syncing is disabled for this provider")
        if request.series and not self.provider.series_enabled:
            self.stats.warn("Series syncing is disabled for this provider")

        if do_movies:
            self._refresh_categories(MOVIE)
            confirmed: list[tuple[CategoryRecord, set[str]]] = []
            for category, folder in self._selected_with_folders(MOVIE):
                seen = self._sync_movie_category(category, folder)
                if seen is not None:
                    confirmed.append((category, seen))
            self._missing_movies(confirmed)
        if do_series:
            self._refresh_categories(SERIES)
            confirmed = []
            for category, folder in self._selected_with_folders(SERIES):
                seen = self._sync_series_category(category, folder, request.full_refresh)
                if seen is not None:
                    confirmed.append((category, seen))
            self._missing_series(confirmed)

    # -- categories -----------------------------------------------------------------------------
    def _refresh_categories(self, content_type: str) -> None:
        warnings: list[str] = []
        try:
            fetch = self.client.get_vod_categories if content_type == MOVIE else self.client.get_series_categories
            cats = fetch(warnings)
        except XtreamError as exc:
            self.stats.warn(f"Could not refresh {content_type} categories, using stored list: {exc}")
            return
        for warning in warnings:
            self.stats.warn(warning)
        if cats:
            self.categories.replace_from_provider(self.provider.id, content_type, [(c.category_id, c.name) for c in cats])

    def _selected_with_folders(self, content_type: str) -> list[tuple[CategoryRecord, str]]:
        """Selected categories with a stable, collision-free folder name each."""
        all_categories = self.categories.list(self.provider.id, content_type)
        selected = sorted((c for c in all_categories if c.selected), key=lambda c: id_sort_key(c.category_id))
        if not selected:
            self.stats.warn(f"No {'Movie' if content_type == MOVIE else 'Series'} categories selected")
            return []
        allocator = NameAllocator()
        for category in sorted(all_categories, key=lambda c: id_sort_key(c.category_id)):
            if category.folder_name:
                allocator.claim(category.folder_name, category.category_id)
        result = []
        for category in selected:
            base = sanitize_component(category.name, max_length=self.rules.max_length)
            candidates = dedupe_candidates(
                [base, with_suffix(base, f" [cat-{category.category_id}]", self.rules.max_length)]
            )
            folder = category.folder_name
            if not (folder and folder in candidates and allocator.owner_of(folder) == category.category_id):
                folder = allocator.allocate(category.category_id, candidates)
                if folder != category.folder_name:
                    self.categories.set_folder_name(self.provider.id, content_type, category.category_id, folder)
            result.append((category, folder))
        return result

    def _absence_confirmed(self, category: CategoryRecord, label: str, listed: int, known: int) -> bool:
        """May items this category's successful answer does not list be treated as missing?

        Not when the provider no longer lists the category itself (renumbered or dropped
        categories must not empty the library), and not for a first empty answer for a
        category that had items: only a second consecutive empty answer confirms it.
        """
        if not category.present:
            self.stats.categories_unconfirmed += 1
            self.stats.warn(f"{label}: the provider no longer lists this category; its items are kept as they are")
            return False
        if listed == 0 and known > 0:
            empty = category.empty_syncs + 1
            self.categories.set_empty_syncs(self.provider.id, category.content_type, category.category_id, empty)
            if empty < 2:
                self.stats.categories_unconfirmed += 1
                self.stats.warn(f"{label}: the provider returned no items; nothing is marked missing unless the "
                                "next sync confirms the category is empty")
                return False
            return True
        if listed > 0 and category.empty_syncs:
            self.categories.set_empty_syncs(self.provider.id, category.content_type, category.category_id, 0)
        return True

    def _category_failed(self, label: str, exc: Exception) -> None:
        self.stats.categories_failed += 1
        message = f"{label}: {exc}"
        self.stats.error(message)
        self.progress.error(message)
        log.error("Category fetch failed – %s", redact(message))

    def _item_failed(self, label: str, exc: Exception) -> None:
        message = f"{label}: {exc}"
        self.stats.error(message)
        self.progress.error(message)
        if isinstance(exc, (OSError, XtreamError)):
            log.error("Item failed – %s", redact(message))
        else:
            log.exception("Item failed – %s", redact(message))

    # -- movies ------------------------------------------------------------------------------------
    def _sync_movie_category(self, category: CategoryRecord, folder: str) -> Optional[set[str]]:
        """Sync one category. Returns the stream IDs it listed when that answer may confirm absence."""
        label = f"Movies / {category.name}"
        self.stats.categories.append(label)
        self.progress.update(phase="Movies", category=category.name)
        category_dir = self.layout.category_dir(MOVIE, folder)
        warnings: list[str] = []
        try:
            streams = self.client.get_vod_streams(category.category_id, warnings)
        except XtreamError as exc:
            self._category_failed(label, exc)
            return None
        for warning in warnings:
            self.stats.warn(f"{label}: {warning}")
            self.progress.warning()

        records = self.state.movies_in_category(self.provider.id, category.category_id)
        allocator = NameAllocator()
        for record in sorted(records.values(), key=lambda r: id_sort_key(r.stream_id)):
            allocator.claim(record.folder_name, record.stream_id)

        unique = _unique_by(streams, lambda s: s.stream_id)
        self.stats.movies_discovered += len(unique)
        self.progress.add_total(len(unique))
        seen: set[str] = set()
        pending: list[MovieRecord] = []
        try:
            for stream in unique:
                self.progress.check_cancelled()
                seen.add(stream.stream_id)
                self.progress.update(current_item=stream.name)
                try:
                    pending.append(self._sync_movie(stream, category, category_dir, records.get(stream.stream_id), allocator))
                except Exception as exc:
                    self.stats.movies_failed += 1
                    self._item_failed(f"{label} / {stream.name or stream.stream_id}", exc)
                self.progress.advance()
                if len(pending) >= BATCH_SIZE:
                    self.state.upsert_movies(pending)
                    pending = []
        finally:
            if pending:
                self.state.upsert_movies(pending)
        self.stats.categories_processed += 1
        return seen if self._absence_confirmed(category, label, len(unique), len(records)) else None

    def _missing_movies(self, confirmed: list[tuple[CategoryRecord, set[str]]]) -> None:
        """Quarantine newly missing movies; purge long-missing ones (absence confirmed again)."""
        for category, seen in confirmed:
            for record in self.state.movies_in_category(self.provider.id, category.category_id).values():
                if record.stream_id in seen:
                    continue
                self.progress.check_cancelled()
                label = f"Movies / {category.name} / {record.title}"
                key = (self.provider.id, category.category_id, record.stream_id)
                if record.status == ACTIVE:
                    self.stats.movies_missing += 1
                if record.status == ACTIVE or record.missing_since is None:
                    self.state.mark_movie_missing(*key, self.now)  # starts the clock once
                    record.missing_since = record.missing_since or self.now
                if record.quarantined_at is None:
                    done, path = self.lifecycle.quarantine(label, record.strm_path)
                    if done:
                        self.state.set_movie_quarantine(*key, self.now, path)
                        self.stats.movies_quarantined += 1 if path else 0
                    continue
                if self.lifecycle.purge_due(record.missing_since):
                    self.lifecycle.purge_movie(record)

    def _movie_folder_foreign(self, category_dir: str, name: str, stream_id: str) -> bool:
        """True when ``name`` already holds another stream's STRM (e.g. from before a DB reset)."""
        url = read_strm_url(os.path.join(category_dir, name, name + ".strm"))
        owner = stream_id_from_url(url) if url else None
        return owner is not None and owner != stream_id

    def _sync_movie(
        self,
        stream: VodStream,
        category: CategoryRecord,
        category_dir: str,
        record: Optional[MovieRecord],
        allocator: NameAllocator,
    ) -> MovieRecord:
        sid = stream.stream_id
        year = resolve_year(stream.name, stream.year, stream.release_date)
        title = clean_title(stream.name, self.rules.clean_titles) or f"Movie {sid}"
        base = movie_base_name(title, year, self.rules)
        label = version_label(stream.name)
        max_len = self.rules.max_length
        candidates = dedupe_candidates([
            base,
            with_suffix(base, f" - {label}", max_len) if label else None,
            with_suffix(base, id_suffix(sid), max_len),
        ])
        if record and record.folder_name in candidates and allocator.owner_of(record.folder_name) == sid:
            folder_name = record.folder_name
        else:
            folder_name = allocator.allocate(sid, candidates, lambda n: self._movie_folder_foreign(category_dir, n, sid))
            if folder_name != base:
                self.stats.name_collisions += 1
                self.stats.warn(f"Name collision: '{base}' exists; movie {sid} stored as '{folder_name}'")
        folder_path = os.path.join(category_dir, folder_name)
        strm_path = os.path.join(folder_path, folder_name + ".strm")
        url = build_movie_url(self.provider.base_url, self.provider.username, self.provider.password, sid, stream.extension)
        url_hash = url_fingerprint(url)
        source_fp = fingerprint(title, year, stream.extension, url_hash, strm_path)

        if record is not None and record.status != ACTIVE:
            self.lifecycle.restore(f"Movie '{title}'", record.strm_path, record.quarantine_path)
            self.stats.movies_restored += 1
        relocated = False
        if record is not None and not _same_path(record.strm_path, strm_path):
            relocated = self._relocate_movie(record, folder_path, folder_name)
        atomic.ensure_dir(folder_path)
        result = write_strm(strm_path, url)

        last_updated = self.now
        if record is None:
            self.stats.movies_created += 1
        elif (
            result is not WriteResult.UNCHANGED
            or relocated
            or record.source_fingerprint != source_fp
            or record.status != ACTIVE
        ):
            self.stats.movies_updated += 1
            if result is WriteResult.CREATED and not relocated:
                self.stats.files_recreated += 1
        else:
            self.stats.movies_unchanged += 1
            last_updated = record.last_updated
        return MovieRecord(
            provider_id=self.provider.id,
            category_id=category.category_id,
            stream_id=sid,
            title=title,
            year=year,
            extension=stream.extension,
            base_name=base,
            folder_name=folder_name,
            folder_path=folder_path,
            strm_path=strm_path,
            url_hash=url_hash,
            source_fingerprint=source_fp,
            status=ACTIVE,
            first_seen=record.first_seen if record else self.now,
            last_seen=self.now,
            last_updated=last_updated,
            last_synced=self.now,
        )

    def _relocate_movie(self, record: MovieRecord, folder_path: str, folder_name: str) -> bool:
        """Move a renamed movie's folder (with its NFO and artwork) instead of starting over."""
        moved = False
        current = record.folder_path
        try:
            if not _same_path(current, folder_path):
                if atomic.is_dir(current) and not atomic.exists(folder_path):
                    moved = atomic.move(current, folder_path)
                    current = folder_path
                    if moved:
                        self.nfo_files.move_folder(self.provider.id, record.folder_path, folder_path)
                else:
                    return False
            if record.folder_name != folder_name:
                moved = rename_sidecars(current, record.folder_name, folder_name) > 0 or moved
        except OSError as exc:
            self.stats.warn(f"Could not move '{record.folder_path}' to '{folder_path}': {exc}")
            return False
        if moved:
            self.stats.items_relocated += 1
            log.info("Relocated movie %s -> %s", record.folder_path, folder_path)
        return moved

    # -- series ------------------------------------------------------------------------------------
    def _sync_series_category(self, category: CategoryRecord, folder: str, full_refresh: bool) -> Optional[set[str]]:
        """Sync one category. Returns the series IDs it listed when that answer may confirm absence."""
        label = f"Series / {category.name}"
        self.stats.categories.append(label)
        self.progress.update(phase="Series", category=category.name)
        category_dir = self.layout.category_dir(SERIES, folder)
        warnings: list[str] = []
        try:
            entries = self.client.get_series(category.category_id, warnings)
        except XtreamError as exc:
            self._category_failed(label, exc)
            return None
        for warning in warnings:
            self.stats.warn(f"{label}: {warning}")
            self.progress.warning()

        records = self.state.series_in_category(self.provider.id, category.category_id)
        allocator = NameAllocator()
        for record in sorted(records.values(), key=lambda r: id_sort_key(r.series_id)):
            allocator.claim(record.folder_name, record.series_id)

        unique = _unique_by(entries, lambda s: s.series_id)
        self.stats.series_discovered += len(unique)
        self.progress.add_total(len(unique))
        seen: set[str] = set()
        for entry in unique:
            self.progress.check_cancelled()
            seen.add(entry.series_id)
            self.progress.update(current_item=entry.name)
            try:
                self._sync_one_series(entry, category, category_dir, records.get(entry.series_id), allocator, full_refresh)
            except JobCancelled:
                raise
            except Exception as exc:
                self.stats.series_failed += 1
                self._item_failed(f"{label} / {entry.name or entry.series_id}", exc)
            self.progress.advance()
        self.stats.categories_processed += 1
        return seen if self._absence_confirmed(category, label, len(unique), len(records)) else None

    def _missing_series(self, confirmed: list[tuple[CategoryRecord, set[str]]]) -> None:
        """A missing series quarantines all its episodes; purging removes the whole series."""
        for category, seen in confirmed:
            for record in self.state.series_in_category(self.provider.id, category.category_id).values():
                if record.series_id in seen:
                    continue
                self.progress.check_cancelled()
                if record.status == ACTIVE:
                    self.stats.series_missing += 1
                if record.status == ACTIVE or record.missing_since is None:
                    self.state.mark_series_missing(self.provider.id, category.category_id, record.series_id, self.now)
                    record.missing_since = record.missing_since or self.now
                episodes = list(self.state.episodes_of_series(self.provider.id, category.category_id,
                                                              record.series_id).values())
                quarantined = self._quarantine_episodes(f"Series / {category.name} / {record.title}", episodes)
                if quarantined and self.lifecycle.purge_due(record.missing_since):
                    self.lifecycle.purge_series(record, episodes)

    def _quarantine_episodes(self, label: str, episodes: Iterable[EpisodeRecord]) -> bool:
        """Mark the episodes missing and quarantine their ``.strm``. True when all are quarantined
        and were so before this sync (only then may a purge follow)."""
        settled = True
        for episode in episodes:
            key = (episode.provider_id, episode.category_id, episode.series_id, episode.episode_id)
            if episode.status == ACTIVE:
                self.stats.episodes_missing += 1
            if episode.status == ACTIVE or episode.missing_since is None:
                self.state.mark_episode_missing(*key, self.now)
                episode.missing_since = episode.missing_since or self.now
            if episode.quarantined_at is None:
                settled = False
                done, path = self.lifecycle.quarantine(f"{label} episode {episode.episode_id}", episode.strm_path)
                if done:
                    self.state.set_episode_quarantine(*key, self.now, path)
                    self.stats.episodes_quarantined += 1 if path else 0
        return settled

    def _can_skip_series(
        self,
        record: Optional[SeriesRecord],
        entry: SeriesEntry,
        series_path: str,
        series_fp: str,
        episodes: dict[str, EpisodeRecord],
        full_refresh: bool,
    ) -> bool:
        """True only when stored state proves the series is unchanged and complete on disk."""
        days = self.settings.series_full_refresh_days
        if full_refresh or days <= 0 or record is None or record.status != ACTIVE:
            return False
        if not entry.last_modified or entry.last_modified in ("0", "") or record.last_modified != entry.last_modified:
            return False
        if record.source_fingerprint != series_fp or not _same_path(record.folder_path, series_path):
            return False
        if older_than(record.info_fetched_at, timedelta(days=days)):
            return False
        active = [e for e in episodes.values() if e.status == ACTIVE]
        if not active or len(active) != record.episode_count:
            return False
        return all(atomic.is_file(e.strm_path) for e in active)

    def _sync_one_series(
        self,
        entry: SeriesEntry,
        category: CategoryRecord,
        category_dir: str,
        record: Optional[SeriesRecord],
        allocator: NameAllocator,
        full_refresh: bool,
    ) -> None:
        sid = entry.series_id
        year = resolve_year(entry.name, entry.year, entry.release_date)
        display = series_display_title(entry.name, self.rules)
        base = series_base_name(entry.name, year, self.rules)
        candidates = dedupe_candidates([base, with_suffix(base, id_suffix(sid), self.rules.max_length)])
        if record and record.folder_name in candidates and allocator.owner_of(record.folder_name) == sid:
            folder_name = record.folder_name
        else:
            folder_name = allocator.allocate(sid, candidates)
            if folder_name != base:
                self.stats.name_collisions += 1
                self.stats.warn(f"Name collision: '{base}' exists; series {sid} stored as '{folder_name}'")
        series_path = os.path.join(category_dir, folder_name)
        if record is not None and not _same_path(record.folder_path, series_path):
            try:
                if atomic.is_dir(record.folder_path) and not atomic.exists(series_path) and atomic.move(record.folder_path, series_path):
                    self.nfo_files.move_folder(self.provider.id, record.folder_path, series_path)
                    self.state.move_episode_paths(self.provider.id, category.category_id, sid, record.folder_path,
                                                  series_path)
                    self.stats.items_relocated += 1
                    log.info("Relocated series %s -> %s", record.folder_path, series_path)
            except OSError as exc:
                self.stats.warn(f"Could not move '{record.folder_path}' to '{series_path}': {exc}")

        series_fp = fingerprint(display, base, series_path, self.rules.episode_title_in_filename, self.credentials_fp)
        episode_records = self.state.episodes_of_series(self.provider.id, category.category_id, sid)

        def save_series(info_fetched: Optional[str], episode_count: int, updated: bool) -> None:
            self.state.upsert_series(SeriesRecord(
                provider_id=self.provider.id,
                category_id=category.category_id,
                series_id=sid,
                title=display,
                year=year,
                base_name=base,
                folder_name=folder_name,
                folder_path=series_path,
                last_modified=entry.last_modified,
                info_fetched_at=info_fetched,
                episode_count=episode_count,
                source_fingerprint=series_fp,
                status=ACTIVE,
                first_seen=record.first_seen if record else self.now,
                last_seen=self.now,
                last_updated=self.now if updated or record is None else record.last_updated,
                last_synced=self.now,
            ))

        if self._can_skip_series(record, entry, series_path, series_fp, episode_records, full_refresh):
            assert record is not None
            active = sum(1 for e in episode_records.values() if e.status == ACTIVE)
            self.state.touch_episodes(self.provider.id, category.category_id, sid, self.now)
            self.stats.series_unchanged += 1
            self.stats.episodes_discovered += active
            self.stats.episodes_unchanged += active
            save_series(record.info_fetched_at, record.episode_count, False)
            return

        try:
            details = self.client.get_series_info(sid)
        except XtreamError as exc:
            self.stats.series_failed += 1
            self._item_failed(f"Series / {category.name} / {display}", exc)
            if record is not None:
                # Still listed by the provider: keep it active, but don't touch episodes.
                save_series(record.info_fetched_at, record.episode_count, False)
            return
        for warning in details.warnings:
            self.stats.warn(f"{display}: {warning}")

        allocators: dict[int, NameAllocator] = defaultdict(NameAllocator)
        for ep_record in sorted(episode_records.values(), key=lambda r: id_sort_key(r.episode_id)):
            allocators[ep_record.season].claim(ep_record.file_name, ep_record.episode_id)
        episodes = sorted(
            details.episodes,
            key=lambda e: (e.season, e.episode_num if e.episode_num is not None else 10**6, id_sort_key(e.episode_id)),
        )
        self.stats.episodes_discovered += len(episodes)
        seen: set[str] = set()
        pending: list[EpisodeRecord] = []
        changed = False
        try:
            for episode in episodes:
                self.progress.check_cancelled()
                seen.add(episode.episode_id)
                try:
                    new_record, was_changed = self._sync_episode(
                        episode, display, series_path, category, sid,
                        episode_records.get(episode.episode_id), allocators[episode.season],
                    )
                    pending.append(new_record)
                    changed = changed or was_changed
                except Exception as exc:
                    self.stats.episodes_failed += 1
                    self._item_failed(f"{display} episode {episode.episode_id}", exc)
        finally:
            if pending:
                self.state.upsert_episodes(pending)
        gone = [e for eid, e in episode_records.items() if eid not in seen]
        newly_missing = [e for e in gone if e.status == ACTIVE]
        if gone and not seen:
            # An answer without a single episode for a show that had some is more likely a
            # provider glitch than a show without episodes: keep its episodes as they are.
            gone = []
            if newly_missing:
                self.stats.warn(f"{display}: the provider listed no episodes; existing episodes are kept as they are")
            newly_missing = []
        self.stats.series_processed += 1
        save_series(self.now, len(pending), changed or bool(newly_missing))
        if gone:
            due = [e for e in gone if e.status != ACTIVE and e.quarantined_at is not None
                   and self.lifecycle.purge_due(e.missing_since)]
            self._quarantine_episodes(f"Series / {category.name} / {display}", gone)
            current = self.state.series_in_category(self.provider.id, category.category_id).get(sid)
            if due and current is not None:
                self.lifecycle.purge_episodes(current, due)

    def _episode_file_foreign(self, season_dir: str, name: str, episode_id: str) -> bool:
        url = read_strm_url(os.path.join(season_dir, name + ".strm"))
        owner = stream_id_from_url(url) if url else None
        return owner is not None and owner != episode_id

    def _sync_episode(
        self,
        episode: Episode,
        display: str,
        series_path: str,
        category: CategoryRecord,
        series_id: str,
        record: Optional[EpisodeRecord],
        allocator: NameAllocator,
    ) -> tuple[EpisodeRecord, bool]:
        eid = episode.episode_id
        if episode.episode_num is None:
            self.stats.warn(f"{display}: episode {eid} has no episode number; using E00")
        season_dir = os.path.join(series_path, season_folder_name(episode.season))
        base = episode_base_name(display, episode.season, episode.episode_num, episode.title, self.rules)
        candidates = dedupe_candidates([base, with_suffix(base, id_suffix(eid), self.rules.max_length)])
        if (
            record
            and record.season == episode.season
            and record.file_name in candidates
            and allocator.owner_of(record.file_name) == eid
        ):
            file_name = record.file_name
        else:
            file_name = allocator.allocate(eid, candidates, lambda n: self._episode_file_foreign(season_dir, n, eid))
            if file_name != base:
                self.stats.name_collisions += 1
                self.stats.warn(f"Name collision: '{base}' exists; episode {eid} stored as '{file_name}'")
        strm_path = os.path.join(season_dir, file_name + ".strm")
        url = build_episode_url(self.provider.base_url, self.provider.username, self.provider.password, eid, episode.extension)
        url_hash = url_fingerprint(url)
        source_fp = fingerprint(episode.season, episode.episode_num, episode.title, episode.extension, url_hash, strm_path)

        if record is not None and record.status != ACTIVE:
            self.lifecycle.restore(f"{display} episode {eid}", record.strm_path, record.quarantine_path)
            self.stats.episodes_restored += 1
        relocated = False
        if record is not None and not _same_path(record.strm_path, strm_path):
            old_dir = os.path.dirname(record.strm_path)
            try:
                if atomic.is_file(record.strm_path) and not atomic.exists(strm_path):
                    atomic.ensure_dir(season_dir)
                    relocated = rename_sidecars(old_dir, record.file_name, file_name, target_dir=season_dir) > 0
                    if relocated:
                        self.nfo_files.move(episode_nfo_path(record.strm_path), episode_nfo_path(strm_path))
                        self.stats.items_relocated += 1
            except OSError as exc:
                self.stats.warn(f"Could not move '{record.strm_path}': {exc}")
        atomic.ensure_dir(season_dir)
        result = write_strm(strm_path, url)

        changed = True
        last_updated = self.now
        if record is None:
            self.stats.episodes_created += 1
        elif result is not WriteResult.UNCHANGED or relocated or record.source_fingerprint != source_fp or record.status != ACTIVE:
            self.stats.episodes_updated += 1
            if result is WriteResult.CREATED and not relocated:
                self.stats.files_recreated += 1
        else:
            self.stats.episodes_unchanged += 1
            last_updated = record.last_updated
            changed = False
        return (
            EpisodeRecord(
                provider_id=self.provider.id,
                category_id=category.category_id,
                series_id=series_id,
                episode_id=eid,
                season=episode.season,
                episode=episode.episode_num if episode.episode_num is not None else 0,
                title=episode.title,
                extension=episode.extension,
                base_name=base,
                file_name=file_name,
                strm_path=strm_path,
                url_hash=url_hash,
                source_fingerprint=source_fp,
                status=ACTIVE,
                first_seen=record.first_seen if record else self.now,
                last_seen=self.now,
                last_updated=last_updated,
                last_synced=self.now,
            ),
            changed,
        )
