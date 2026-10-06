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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Final, Iterable, Iterator, Literal, Optional, TypeVar

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
from ..xtream.parsing import ParsedRecords
from ..xtream.models import Episode, SeriesDetails, SeriesEntry, VodStream
from ..xtream.urls import build_episode_url, build_movie_url, stream_id_from_url
from ..nfo.paths import episode_nfo_path
from .lifecycle import MissingItemLifecycle
from .stats import SyncStats

log = logging.getLogger(__name__)

T = TypeVar("T")
BATCH_SIZE: Final = 200

SyncType = Literal["all", "movies", "series"]
# How a provider's sync ended (stored in the sync history).
SyncStatus = Literal["success", "partial", "failed", "cancelled"]


class FatalSyncError(Exception):
    """Stops the provider's sync (bad credentials, no target folder...)."""


@dataclass
class SyncRequest:
    movies: bool = True
    series: bool = True
    full_refresh: bool = False  # ignore the "series unchanged" shortcut

    @property
    def sync_type(self) -> SyncType:
        if self.movies and self.series:
            return "all"
        return "movies" if self.movies else "series"


def id_sort_key(value: str) -> tuple[int, int, str]:
    """Numeric IDs first, in numeric order, then text IDs. ``isdecimal``, not ``isdigit``:
    superscript or circled digits are digits that ``int()`` refuses."""
    return (0, int(value), "") if value.isdecimal() else (1, 0, value)


def _unique_by(items: Iterable[T], key: Callable[[T], str]) -> list[T]:
    seen: set[str] = set()
    result: list[T] = []
    for item in items:
        k = key(item)
        if k not in seen:
            seen.add(k)
            result.append(item)
    return sorted(result, key=lambda i: id_sort_key(key(i)))


def _unparsed_ids(listing: ParsedRecords[T], parsed: Iterable[str]) -> set[str]:
    """IDs a catalog answer lists but could not parse (see ``ParsedRecords``)."""
    unparsed = getattr(listing, "unparsed_ids", None) or set()
    return set(unparsed) - set(parsed)


def _owned_backup(path: Optional[str], old_strm: str, new_strm: str) -> Optional[str]:
    """Where a ``.bak`` the application still owns is now, or None when it is gone.

    A backup ``<old>.strm[.N].bak`` follows a relocated ``.strm`` (folder move and sidecar
    rename keep the part after the ``.strm`` name).

    Contract:
        Preconditions:
            - ``path`` and ``old_strm`` are spelled the same way (both as stored by the sync).
              The follow-the-move step is a plain string-prefix match: a difference in case,
              separators or a Windows long-path prefix silently yields ``None``.
        Silences:
            - ``None`` covers both "deleted by hand" and "moved somewhere it cannot follow";
              the caller then forgets the backup.
    """
    if not path:
        return None
    if atomic.is_file(path):
        return path
    if not _same_path(old_strm, new_strm) and path.startswith(old_strm):
        moved = new_strm + path[len(old_strm):]
        if atomic.is_file(moved):
            return moved
    return None


def _all_files_exist(paths: Iterable[str]) -> bool:
    """True when every path is an existing file: one directory scan per folder instead of a
    ``stat`` per file (a deleted ``.strm`` must still be noticed and recreated).

    Contract:
        Preconditions:
            - Names are compared with ``os.path.normcase``, so on POSIX a file that differs
              only in case counts as missing (fails safe: the series is not skipped).
            - An empty ``paths`` is vacuously True; the caller must rule that out itself
              (``_can_skip_series`` requires at least one active episode).
        Silences:
            - Any ``OSError`` from scanning a folder (missing, permission denied) returns
              False, which is the safe answer ("not proven complete").
    """
    by_folder: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        folder, name = os.path.split(path)
        by_folder[folder].add(os.path.normcase(name))
    for folder, names in by_folder.items():
        try:
            with os.scandir(atomic.fs_path(folder)) as entries:
                files = {os.path.normcase(e.name) for e in entries if e.is_file()}
        except OSError:
            return False
        if not names <= files:
            return False
    return True


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
    def run(self, request: SyncRequest) -> tuple[SyncStatus, SyncStats]:
        """Sync this provider once and return how it ended plus its statistics.

        Contract:
            Raises:
                BaseException: Only non-``Exception`` exits other than ``JobCancelled``
                    (``KeyboardInterrupt``, ``SystemExit``) escape; the client is still closed.
            Silences:
                - ``JobCancelled`` -> ``"cancelled"`` with a warning; writes made before the
                  cancel are kept (see ``_one_commit`` and the batch flushes).
                - ``FatalSyncError`` and every other ``Exception`` -> ``"failed"``, the redacted
                  message in ``stats.errors`` and the log. Partial work done before the error
                  stays on disk and in the database.
                - A run that recorded any error in ``stats.errors`` (failed category or item)
                  is ``"partial"``, never ``"success"``.
        """
        status: SyncStatus = "success"
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
        """Movies, then series: sync every selected category, then judge absence per type.

        Contract:
            Raises:
                FatalSyncError: No target folder, the provider folder cannot be created, the
                    provider is unreachable (``XtreamError`` from ``authenticate``) or refuses
                    the login.
                JobCancelled: From ``progress.check_cancelled`` between items.
                sqlite3.Error: From the state repositories (not caught here; ``run`` does).
        """
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
        fetch = self.client.get_vod_categories if content_type == MOVIE else self.client.get_series_categories
        try:
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

        Contract:
            Preconditions:
                - Called once per successful answer, never for a failed request: each call
                  with ``listed == 0`` advances ``empty_syncs``.
                - ``listed`` counts listed-but-unparsed records too, and ``known`` counts every
                  stored row of the category whatever its status; otherwise a category of
                  malformed records would read as empty.
            Raises:
                sqlite3.Error: From storing ``empty_syncs`` (not caught).
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

    @contextmanager
    def _one_commit(self) -> Iterator[None]:
        """Group many small state writes into one transaction.

        Unlike ``db.transaction()``, writes made before an exception (e.g. a cancellation)
        are still committed: they record files that were already renamed on disk.

        Contract:
            Raises:
                BaseException: Whatever the block raised, re-raised after the commit.
                sqlite3.Error: From ``BEGIN IMMEDIATE`` or ``COMMIT``; a failed commit loses
                    the block's writes while its file renames stay done.
            Silences:
                - Nothing, but the commit-on-error is load-bearing: replacing this with
                  ``db.transaction()`` would roll back the rows that record ``.bak`` files
                  already on disk. The write lock is held for the whole block, file I/O
                  included.
        """
        error: Optional[BaseException] = None
        with self.db.transaction():
            try:
                yield
            except BaseException as exc:
                error = exc
        if error is not None:
            raise error

    # -- movies ------------------------------------------------------------------------------------
    def _sync_movie_category(self, category: CategoryRecord, folder: str) -> Optional[set[str]]:
        """Sync one category. Returns the stream IDs it listed when that answer may confirm absence.

        Contract:
            Raises:
                JobCancelled: Between items; the records already synced are still stored.
                sqlite3.Error: From reading or batch-storing the state.
            Silences:
                - ``XtreamError`` from the listing: counted as a failed category, returns
                  ``None`` (nothing in it may become missing).
                - Any ``Exception`` from one movie: counted in ``movies_failed`` and
                  ``stats.errors``; that movie's row is not rewritten (it keeps its previous
                  state, apart from a restore already recorded) and it still counts as listed.
                - ``None`` also means "answer not trusted for absence" (see
                  ``_absence_confirmed``); callers must never treat ``None`` as an empty set.
        """
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
        unparsed = _unparsed_ids(streams, (s.stream_id for s in unique))
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
                except JobCancelled:
                    raise
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
        seen |= unparsed  # listed but unparsed: kept as they are, never missing
        self.stats.categories_processed += 1
        listed = len(unique) + len(unparsed)
        return seen if self._absence_confirmed(category, label, listed, len(records)) else None

    def _missing_movies(self, confirmed: list[tuple[CategoryRecord, set[str]]]) -> None:
        """Quarantine newly missing movies; purge long-missing ones (absence confirmed again)."""
        for category, seen in confirmed:
            gone = [r for r in self.state.movies_in_category(self.provider.id, category.category_id).values()
                    if r.stream_id not in seen]
            if gone:
                with self._one_commit():
                    self._missing_movies_of(category, gone)

    def _missing_movies_of(self, category: CategoryRecord, gone: list[MovieRecord]) -> None:
        """Mark missing, quarantine once, and purge on a later confirming sync.

        Contract:
            Preconditions:
                - ``gone`` holds rows of a category whose answer confirmed absence in this
                  sync, and the caller wraps this in ``_one_commit``.
                - ``quarantined_at`` is trusted over the disk: a row that claims a quarantine
                  is never quarantined again (a restore clears it at once, see ``_restore``).
            Silences:
                - A quarantine that fails (``done`` False) only warns; ``quarantined_at`` stays
                  empty, so the next confirming sync retries it.
                - A movie quarantined in this sync is never purged in the same sync (the
                  ``continue``): the purge needs a later confirmation.
        """
        for record in gone:
            self.progress.check_cancelled()
            label = f"Movies / {category.name} / {record.title}"
            key = (self.provider.id, category.category_id, record.stream_id)
            if record.status == ACTIVE:
                self.stats.movies_missing += 1
            if record.status == ACTIVE or record.missing_since is None:
                self.state.mark_movie_missing(*key, self.now)  # starts the clock once
                record.missing_since = record.missing_since or self.now
            if record.quarantined_at is None:
                done, path = self.lifecycle.quarantine(label, record.strm_path, record.quarantine_path)
                if done:
                    self.state.set_movie_quarantine(*key, self.now, path)
                    self.stats.movies_quarantined += 1 if path else 0
                continue
            if self.lifecycle.purge_due(record.missing_since):
                self.lifecycle.purge_movie(record)

    def _restore(self, label: str, record: MovieRecord | EpisodeRecord) -> tuple[Optional[str], bool]:
        """Restore a returning item's quarantined ``.strm``.

        Returns ``(backup still owned, restored)``: ``restored`` is True only when the ``.bak``
        was actually moved back (not when it was gone already, or the restore failed or was
        skipped because a ``.strm`` exists again).
        """
        had_backup = record.quarantine_path is not None and atomic.is_file(record.quarantine_path)
        backup = self.lifecycle.restore(label, record.strm_path, record.quarantine_path)
        return backup, had_backup and backup is None and atomic.is_file(record.strm_path)

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
        """Name, restore, relocate and write one movie; return the record to store.

        Contract:
            Preconditions:
                - ``allocator`` already holds every stored folder name of the category, so the
                  stored name of ``record`` is kept while it is still a valid candidate.
            Raises:
                OSError: From creating the folder or writing the ``.strm`` (not caught; the
                    category loop counts the movie as failed).
                sqlite3.Error: From recording a restore or a relocated NFO.
            Silences:
                - On a raise the work already done stays: a restored ``.bak`` (recorded at
                  once), a relocated folder, the claimed name and the counters
                  (``name_collisions``, ``items_relocated``, ``movies_restored``).
                - The returned record is not stored here; the caller batches it.
        """
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

        # A .bak the application still owns (restore failed or was skipped) stays recorded.
        backup = record.quarantine_path if record is not None else None
        if record is not None and record.status != ACTIVE:
            backup, restored = self._restore(f"Movie '{title}'", record)
            if restored:
                # Recorded at once: a failure later in this item must not leave the row
                # claiming a quarantine while the .strm is live (it would never be quarantined
                # again, and a purge would orphan the .strm).
                self.state.clear_movie_missing(record.provider_id, record.category_id, record.stream_id)
                self.stats.movies_restored += 1
        relocated = False
        if record is not None and not _same_path(record.strm_path, strm_path):
            relocated = self._relocate_movie(record, folder_path, folder_name)
        atomic.ensure_dir(folder_path)
        result = write_strm(strm_path, url)
        backup = _owned_backup(backup, record.strm_path, strm_path) if record is not None else None

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
            quarantine_path=backup,
        )

    def _relocate_movie(self, record: MovieRecord, folder_path: str, folder_name: str) -> bool:
        """Move a renamed movie's folder (with its NFO and artwork) instead of starting over.

        Returns True when a folder or at least one sidecar was moved.

        Contract:
            Raises:
                sqlite3.Error: From moving the NFO ownership rows.
            Silences:
                - ``OSError`` from the move: a warning, False.
                - An existing target folder returns False without a warning; the caller then
                  writes the ``.strm`` in the new folder and the old folder stays as it is.
                - A sidecar that cannot be renamed is skipped by ``rename_sidecars`` (logged
                  only).
        """
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
        """Sync one category. Returns the series IDs it listed when that answer may confirm absence.

        Contract:
            Raises:
                JobCancelled: Between series; the series records already synced are stored.
                sqlite3.Error: From reading or batch-storing the state.
            Silences:
                - As ``_sync_movie_category``: a failed listing returns ``None``; any
                  ``Exception`` from one series is counted in ``series_failed`` and the series
                  still counts as listed.
        """
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
        unparsed = _unparsed_ids(entries, (s.series_id for s in unique))
        self.stats.series_discovered += len(unique)
        self.progress.add_total(len(unique))
        episodes_by_series = self.state.episodes_in_category(self.provider.id, category.category_id)
        seen: set[str] = set()
        pending: list[SeriesRecord] = []
        try:
            for entry in unique:
                self.progress.check_cancelled()
                seen.add(entry.series_id)
                self.progress.update(current_item=entry.name)
                try:
                    self._sync_one_series(entry, category, category_dir, records.get(entry.series_id), allocator,
                                          full_refresh, episodes_by_series.get(entry.series_id, {}), pending.append)
                except JobCancelled:
                    raise
                except Exception as exc:
                    self.stats.series_failed += 1
                    self._item_failed(f"{label} / {entry.name or entry.series_id}", exc)
                self.progress.advance()
                if len(pending) >= BATCH_SIZE:
                    self.state.upsert_series_many(pending)
                    pending = []
        finally:
            if pending:
                self.state.upsert_series_many(pending)
        seen |= unparsed  # listed but unparsed: kept as they are, never missing
        self.stats.categories_processed += 1
        listed = len(unique) + len(unparsed)
        return seen if self._absence_confirmed(category, label, listed, len(records)) else None

    def _missing_series(self, confirmed: list[tuple[CategoryRecord, set[str]]]) -> None:
        """A missing series quarantines all its episodes; purging removes the whole series."""
        for category, seen in confirmed:
            gone = [r for r in self.state.series_in_category(self.provider.id, category.category_id).values()
                    if r.series_id not in seen]
            if gone:
                with self._one_commit():
                    self._missing_series_of(category, gone)

    def _missing_series_of(self, category: CategoryRecord, gone: list[SeriesRecord]) -> None:
        """Mark each vanished series missing, quarantine all its episodes, purge when due.

        Contract:
            Preconditions:
                - As ``_missing_movies_of`` (confirmed category, inside ``_one_commit``). A
                  series is purged only when every episode was already quarantined before
                  this sync (``_quarantine_episodes`` returns True) and it is overdue.
        """
        for record in gone:
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
        and were so before this sync (only then may a purge follow).

        Contract:
            Silences:
                - A failed quarantine only warns (see ``MissingItemLifecycle.quarantine``); the
                  result is False either way, so a purge waits.
                - An empty ``episodes`` returns True: a series without stored episodes may be
                  purged at once when overdue.
        """
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
                done, path = self.lifecycle.quarantine(f"{label} episode {episode.episode_id}", episode.strm_path,
                                                       episode.quarantine_path)
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
        """True only when stored state proves the series is unchanged and complete on disk.

        Contract:
            Silences:
                - Every doubt (no ``last_modified``, a stale info fetch, a count mismatch, a
                  missing file or an unreadable folder) answers False, so the series is fully
                  synced; a wrong False only costs time.
        """
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
        return _all_files_exist(e.strm_path for e in active)

    def _sync_one_series(
        self,
        entry: SeriesEntry,
        category: CategoryRecord,
        category_dir: str,
        record: Optional[SeriesRecord],
        allocator: NameAllocator,
        full_refresh: bool,
        episode_records: dict[str, EpisodeRecord],
        save: Callable[[SeriesRecord], None],
    ) -> None:
        """Sync one series. ``episode_records``: its stored episodes (preloaded per category);
        ``save`` receives the series record to store (batched by the caller).

        Contract:
            Raises:
                JobCancelled: Between episodes (the episodes synced so far are stored).
                sqlite3.Error: From the state repositories.
            Silences:
                - ``XtreamError`` from the series info: counted in ``series_failed``; a known
                  series is saved unchanged (still active), its episodes are not touched.
                - An answer without any episode for a series that had some marks nothing
                  missing (a warning when that would have happened).
                - Every save resets the series to active with no ``missing_since``: a series
                  that is listed again ends its missing lifecycle here.
        """
        sid = entry.series_id
        year = resolve_year(entry.name, entry.year, entry.release_date)
        display = series_display_title(entry.name, self.rules)
        base = series_base_name(entry.name, year, self.rules)
        folder_name = self._allocate_series_folder(sid, base, record, allocator)
        series_path = os.path.join(category_dir, folder_name)
        if record is not None and not _same_path(record.folder_path, series_path):
            if self._relocate_series(record, category, series_path):
                # The preloaded episode paths are stale now.
                episode_records = self.state.episodes_of_series(self.provider.id, category.category_id, sid)

        series_fp = fingerprint(display, base, series_path, self.rules.episode_title_in_filename, self.credentials_fp)

        def save_series(info_fetched: Optional[str], episode_count: int, updated: bool) -> SeriesRecord:
            saved = SeriesRecord(
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
            )
            save(saved)
            return saved

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

        seen, saved_count, changed = self._sync_episodes(details, display, series_path, category, sid,
                                                         episode_records)
        # Listed but unparsed episodes are kept as they are, never missing.
        seen |= details.unparsed_episode_ids
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
        current = save_series(self.now, saved_count, changed or bool(newly_missing))
        if gone:
            self._handle_gone_episodes(f"Series / {category.name} / {display}", current, gone)

    def _allocate_series_folder(self, sid: str, base: str, record: Optional[SeriesRecord],
                                allocator: NameAllocator) -> str:
        """The series' folder name: its stored name while still valid, else a new allocation."""
        candidates = dedupe_candidates([base, with_suffix(base, id_suffix(sid), self.rules.max_length)])
        if record and record.folder_name in candidates and allocator.owner_of(record.folder_name) == sid:
            return record.folder_name
        folder_name = allocator.allocate(sid, candidates)
        if folder_name != base:
            self.stats.name_collisions += 1
            self.stats.warn(f"Name collision: '{base}' exists; series {sid} stored as '{folder_name}'")
        return folder_name

    def _relocate_series(self, record: SeriesRecord, category: CategoryRecord, series_path: str) -> bool:
        """Move a renamed series' folder (NFOs, artwork, episodes). True when it was moved.

        Contract:
            Silences:
                - ``OSError`` from the move: a warning, False.
                - An existing target folder or a missing source folder: False without a
                  warning; each episode is then moved on its own by ``_sync_episode``.
            Raises:
                sqlite3.Error: From moving the NFO rows or the stored episode paths; the
                    folder has already moved then.
        """
        try:
            if (atomic.is_dir(record.folder_path) and not atomic.exists(series_path)
                    and atomic.move(record.folder_path, series_path)):
                self.nfo_files.move_folder(self.provider.id, record.folder_path, series_path)
                self.state.move_episode_paths(self.provider.id, category.category_id, record.series_id,
                                              record.folder_path, series_path)
                self.stats.items_relocated += 1
                log.info("Relocated series %s -> %s", record.folder_path, series_path)
                return True
        except OSError as exc:
            self.stats.warn(f"Could not move '{record.folder_path}' to '{series_path}': {exc}")
        return False

    def _sync_episodes(
        self,
        details: SeriesDetails,
        display: str,
        series_path: str,
        category: CategoryRecord,
        sid: str,
        episode_records: dict[str, EpisodeRecord],
    ) -> tuple[set[str], int, bool]:
        """Write every listed episode. Returns (episode IDs seen, records saved, anything changed).

        Contract:
            Raises:
                JobCancelled: Between episodes; the records already built are stored first.
            Silences:
                - Any ``Exception`` from one episode: counted in ``episodes_failed``; the
                  episode is still "seen" (never missing) and its row keeps its previous
                  state.
        """
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
                except JobCancelled:
                    raise
                except Exception as exc:
                    self.stats.episodes_failed += 1
                    self._item_failed(f"{display} episode {episode.episode_id}", exc)
        finally:
            if pending:
                self.state.upsert_episodes(pending)
        return seen, len(pending), changed

    def _handle_gone_episodes(self, label: str, series: SeriesRecord, gone: list[EpisodeRecord]) -> None:
        """Quarantine episodes the answer no longer lists; purge those missing long enough.

        Contract:
            Preconditions:
                - ``gone`` comes from an answer that listed at least one episode (an empty
                  episode list never gets here).
                - Which episodes are due is decided before quarantining, so an episode
                  quarantined in this sync is never purged in the same sync.
        """
        due = [e for e in gone if e.status != ACTIVE and e.quarantined_at is not None
               and self.lifecycle.purge_due(e.missing_since)]
        with self._one_commit():
            self._quarantine_episodes(label, gone)
            if due:
                self.lifecycle.purge_episodes(series, due)

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
        """Name, restore, relocate and write one episode; return its record and whether it changed.

        Contract:
            Preconditions:
                - ``allocator`` is the season's allocator, preloaded with the stored names.
            Raises:
                OSError: From creating the season folder or writing the ``.strm``.
                sqlite3.Error: From recording a restore or a moved NFO.
            Silences:
                - ``OSError`` while moving the old files to a new name: a warning, the new
                  ``.strm`` is written anyway and the old files stay.
                - On a raise, as ``_sync_movie``: a restore is already recorded, counters stay.
        """
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

        # A .bak the application still owns (restore failed or was skipped) stays recorded.
        backup = record.quarantine_path if record is not None else None
        if record is not None and record.status != ACTIVE:
            backup, restored = self._restore(f"{display} episode {eid}", record)
            if restored:
                # Recorded at once, as for movies (see _sync_movie).
                self.state.clear_episode_missing(record.provider_id, record.category_id, record.series_id,
                                                 record.episode_id)
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
        backup = _owned_backup(backup, record.strm_path, strm_path) if record is not None else None

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
                quarantine_path=backup,
            ),
            changed,
        )
