"""The missing-item lifecycle: reversible ``.strm`` quarantine, restore, and permanent purge.

* **Quarantine.** When a successful provider answer confirms that an item is gone, only its
  ``.strm`` is renamed to ``<name>.strm.bak`` (media servers stop showing it). NFO files,
  artwork and every other file stay as they are. An existing ``.bak`` is never overwritten.
* **Restore.** When the item comes back, the ``.bak`` is renamed back; normal STRM writing
  then corrects the URL.
* **Purge** (only when enabled, after the configured delay, and only during a sync that
  confirms the absence again). Deletes only what the application can prove it owns: the
  quarantined ``.strm``, managed artwork (artwork ownership rules) and NFOs the application
  created that are unchanged since its last write. Then empty folders are removed, never
  recursively. The item's database state goes last, so a failed deletion is retried by a
  later sync.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Final, Iterator, Optional

from ..artwork.manager import ArtworkManager, ArtworkStats
from ..artwork.naming import ArtworkTarget
from ..config.settings import Settings
from ..filesystem import atomic
from ..filesystem.naming import season_folder_name
from ..library.models import EPISODE, MOVIE, LibraryItem, LibrarySeries
from ..storage.db import Database
from ..storage.nfo_files import MANAGED, ItemKey, NfoFileRepository
from ..storage.purge import PurgeRepository
from ..storage.sync_state import EpisodeRecord, MovieRecord, SeriesRecord
from ..utils.hashing import file_sha256
from ..utils.timeutil import parse_iso, utcnow
from .stats import SyncStats

log = logging.getLogger(__name__)

QUARANTINE_SUFFIX: Final = ".bak"
MAX_QUARANTINE_NAMES: Final = 100


def quarantine_names(strm_path: str) -> Iterator[str]:
    """``<name>.strm.bak``, then ``<name>.strm.2.bak`` ... (never an existing file)."""
    yield strm_path + QUARANTINE_SUFFIX
    for n in range(2, MAX_QUARANTINE_NAMES + 1):
        yield f"{strm_path}.{n}{QUARANTINE_SUFFIX}"


def _real(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _within(path: str, folder: str) -> bool:
    """``path`` is ``folder`` itself or below it (symbolic links resolved)."""
    real, root = _real(path), _real(folder).rstrip(os.sep)
    return real == root or real.startswith(root + os.sep)


def _in_folder(path: str, folder: str) -> bool:
    """``path`` sits directly in ``folder`` and is not a symbolic link."""
    return not os.path.islink(path) and _real(os.path.dirname(path)) == _real(folder)


class MissingItemLifecycle:
    def __init__(self, db: Database, settings: Settings, provider_dir: str, library_root: str,
                 stats: SyncStats) -> None:
        self.db = db
        self.settings = settings
        self.provider_dir = provider_dir
        self.library_root = library_root
        self.stats = stats
        self.nfo_files = NfoFileRepository(db)
        self.purges = PurgeRepository(db)

    # -- quarantine / restore ------------------------------------------------------------------------
    def quarantine(self, label: str, strm_path: str,
                   owned_backup: Optional[str] = None) -> tuple[bool, Optional[str]]:
        """Rename the item's ``.strm`` out of the media server's view.

        Returns ``(done, quarantine path)``. ``done`` is False after an I/O error: the item
        stays unquarantined and the next confirming sync tries again. A ``.strm`` that no
        longer exists (deleted by the user) leaves nothing to quarantine: ``(True, None)``.

        ``owned_backup`` is a ``.bak`` the application still owns from an earlier quarantine
        whose restore did not happen (see :meth:`restore`). The ``.strm`` being quarantined now
        supersedes it, so it is deleted first (with the usual ownership guards); without a
        ``.strm`` it simply stays the item's quarantine path.

        Contract:
            Preconditions:
                - ``strm_path`` is the item's own ``.strm`` as recorded by the sync; whatever
                  file is there is renamed.
            Silences:
                - ``OSError`` from the rename, a leftover backup that cannot be deleted, and
                  100 existing backup names all return ``(False, None)`` with a warning (the
                  leftover case logs an error). Nothing is renamed then.
                - A backup name that appears between the existence check and the rename makes
                  ``atomic.move`` return False; the next name is tried.
        """
        leftover = owned_backup if owned_backup and atomic.is_file(owned_backup) else None
        if not atomic.is_file(strm_path):
            return True, leftover
        if leftover and not self._delete_quarantined(label, leftover, os.path.dirname(strm_path), count=False):
            return False, None
        for candidate in quarantine_names(strm_path):
            if atomic.exists(candidate):
                continue
            try:
                if atomic.move(strm_path, candidate):
                    log.info("Quarantined missing item %s -> %s", strm_path, os.path.basename(candidate))
                    return True, candidate
            except OSError as exc:
                self.stats.warn(f"{label}: could not quarantine {os.path.basename(strm_path)}: {exc}")
                return False, None
        self.stats.warn(f"{label}: could not quarantine {os.path.basename(strm_path)}: too many existing backups")
        return False, None

    def restore(self, label: str, strm_path: str, quarantine_path: Optional[str]) -> Optional[str]:
        """The item is back: put its quarantined ``.strm`` back (the caller rewrites the URL).

        Returns the quarantine path the application still owns afterwards: ``None`` once
        the ``.bak`` was moved back (or is gone), else ``quarantine_path`` (restore failed,
        or a ``.strm`` exists again), so the caller keeps it and a later quarantine or
        purge can still clean it up.

        Contract:
            Silences:
                - ``OSError`` from the rename: a warning; the backup stays owned.
                - ``None`` does not say whether a ``.bak`` was moved back or was already gone;
                  callers that need to know check the ``.bak`` first (``SyncEngine._restore``).
        """
        if not quarantine_path or not atomic.is_file(quarantine_path):
            return None  # removed by hand: the caller simply recreates the .strm
        if atomic.exists(strm_path):
            self.stats.warn(f"{label}: {os.path.basename(strm_path)} already exists again; "
                            f"{os.path.basename(quarantine_path)} was left untouched")
            return quarantine_path
        try:
            if atomic.move(quarantine_path, strm_path):
                return None
        except OSError as exc:
            self.stats.warn(f"{label}: could not restore {os.path.basename(quarantine_path)}: {exc}")
        return quarantine_path if atomic.is_file(quarantine_path) else None

    # -- purge eligibility ---------------------------------------------------------------------------
    def purge_due(self, missing_since: Optional[str]) -> bool:
        """Missing for at least the configured number of days (purging enabled).

        Contract:
            Silences:
                - An empty or unparseable ``missing_since`` is never due (no purge without a
                  known start of the missing period).
        """
        days = self.settings.missing_purge_days
        since = parse_iso(missing_since)
        return days > 0 and since is not None and utcnow() - since >= timedelta(days=days)

    # -- purge -----------------------------------------------------------------------------------------
    def purge_movie(self, record: MovieRecord) -> bool:
        """Delete what the application owns of a missing movie, then forget it.

        Contract:
            Preconditions:
                - The movie is missing and quarantined, and the current sync confirmed its
                  absence again; this method does not check that (the caller does).
                - A live ``.strm`` in the folder is not the application's to delete here: it
                  stays, and so does the folder, while the row is still forgotten.
            Raises:
                sqlite3.Error: From the NFO ownership rows or the final purge transaction.
            Silences:
                - A folder outside this provider's library: a warning, False, nothing deleted.
                - A failed deletion: an error, ``purge_failures`` + 1, False; the database
                  state stays so a later sync retries. Files deleted before that stay deleted.
        """
        label = f"Movie '{record.title}'"
        folder = record.folder_path
        if not self._inside_library(label, folder):
            return False
        ok = self._delete_quarantined(label, record.quarantine_path, folder)
        item = LibraryItem(MOVIE, record.provider_id, record.category_id, record.stream_id, record.title,
                           record.strm_path, status=record.status, year=record.year)
        ok = self._purge_artwork(ArtworkTarget.for_movie(item)) and ok
        ok = self._delete_nfos(label, (record.provider_id, MOVIE, record.category_id, record.stream_id), [folder]) and ok
        if not ok:
            self.stats.purge_failures += 1
            return False
        self._remove_empty_dir(folder)
        self.purges.purge_movie(record.provider_id, record.category_id, record.stream_id)
        self.stats.movies_purged += 1
        log.info("Purged missing movie %s (%s)", record.title, folder)
        return True

    def purge_episodes(self, series: SeriesRecord, episodes: list[EpisodeRecord]) -> int:
        """Purge single episodes of a series that is still listed. Returns how many were purged.

        Contract:
            Preconditions:
                - As :meth:`purge_movie`, for each episode.
            Silences:
                - A series folder outside the library returns 0 with a warning; an episode
                  that cannot be fully cleaned counts in ``purge_failures`` and keeps its
                  state while the others are purged.
        """
        if not self._inside_library(f"Series '{series.title}'", series.folder_path):
            return 0
        done = []
        for episode in episodes:
            if self._purge_episode_files(series, episode):
                done.append(episode.episode_id)
                self._remove_empty_dir(os.path.dirname(episode.strm_path))
            else:
                self.stats.purge_failures += 1
        if done:
            self.purges.purge_episodes(series.provider_id, series.category_id, series.series_id, done)
            self.stats.episodes_purged += len(done)
        return len(done)

    def purge_series(self, series: SeriesRecord, episodes: list[EpisodeRecord]) -> bool:
        """Purge a whole missing series: episodes, seasons, the show, then its state.

        Contract:
            Preconditions:
                - As :meth:`purge_movie`; ``episodes`` are all stored episodes of the series.
            Silences:
                - Season IDs that do not end in ``/<number>`` are skipped.
                - Any failed deletion keeps the whole series' state (``purge_failures`` + 1);
                  files deleted before that stay deleted.
        """
        label = f"Series '{series.title}'"
        folder = series.folder_path
        if not self._inside_library(label, folder):
            return False
        ok = True
        for episode in episodes:
            ok = self._purge_episode_files(series, episode) and ok
        show = LibrarySeries(series.provider_id, series.category_id, series.series_id, series.title, series.year,
                             folder, status=series.status)
        season_dirs = {os.path.dirname(e.strm_path) for e in episodes}
        for season_id in self.purges.season_ids(series.provider_id, series.category_id, series.series_id):
            _, sep, number_text = season_id.partition("/")
            if not sep:
                continue
            try:
                number = int(number_text)
            except ValueError:
                continue
            season_dir = os.path.join(folder, season_folder_name(number))
            season_dirs.add(season_dir)
            ok = self._purge_artwork(ArtworkTarget.for_season(show, season_dir, number)) and ok
            ok = self._delete_nfos(label, (series.provider_id, "season", series.category_id, season_id),
                                   [season_dir]) and ok
        ok = self._purge_artwork(ArtworkTarget.for_series(show)) and ok
        ok = self._delete_nfos(label, (series.provider_id, "series", series.category_id, series.series_id),
                               [folder]) and ok
        if not ok:
            self.stats.purge_failures += 1
            return False
        for season_dir in sorted(season_dirs, key=len, reverse=True):  # bottom-up
            if _within(season_dir, folder):
                self._remove_empty_dir(season_dir)
        self._remove_empty_dir(folder)
        self.purges.purge_series(series.provider_id, series.category_id, series.series_id)
        self.stats.series_purged += 1
        self.stats.episodes_purged += len(episodes)
        log.info("Purged missing series %s (%s)", series.title, folder)
        return True

    # -- helpers -----------------------------------------------------------------------------------------
    def _purge_episode_files(self, series: SeriesRecord, episode: EpisodeRecord) -> bool:
        label = f"{series.title} episode {episode.episode_id}"
        season_dir = os.path.dirname(episode.strm_path)
        if not _within(season_dir, series.folder_path):
            self.stats.warn(f"{label}: not inside the series folder; nothing deleted")
            return False
        ok = self._delete_quarantined(label, episode.quarantine_path, season_dir)
        item = LibraryItem(EPISODE, episode.provider_id, episode.category_id, episode.episode_id, episode.title,
                           episode.strm_path, status=episode.status, series_id=series.series_id,
                           series_title=series.title, series_folder=series.folder_path, season=episode.season,
                           episode=episode.episode)
        ok = self._purge_artwork(ArtworkTarget.for_episode(item)) and ok
        return self._delete_nfos(label, (episode.provider_id, EPISODE, episode.category_id, episode.episode_id),
                                 [season_dir]) and ok

    def _inside_library(self, label: str, folder: str) -> bool:
        if _within(folder, self.provider_dir) and not _real(folder) == _real(self.provider_dir):
            return True
        self.stats.warn(f"{label}: folder is outside this provider's library folder; nothing deleted")
        return False

    def _delete_quarantined(self, label: str, path: Optional[str], folder: str, count: bool = True) -> bool:
        """The quarantined ``.strm`` is the application's own file: delete it.

        Contract:
            Silences:
                - A ``.bak`` that is not directly in ``folder``, is a symbolic link or does not
                  end in ``.bak`` is kept with a warning and the result is still True, so the
                  purge goes ahead and the file is no longer recorded as the application's.
                - A file already gone counts as deleted. Other ``OSError`` -> an error, False.
        """
        if not path or not atomic.is_file(path):
            return True
        if not _in_folder(path, folder) or not os.path.basename(path).lower().endswith(".bak"):
            self.stats.warn(f"{label}: {os.path.basename(path)} is not where the application put it; kept")
            return True
        try:
            os.remove(atomic.fs_path(path))
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.stats.error(f"{label}: {os.path.basename(path)} could not be deleted: {exc}")
            return False
        if count:
            self.stats.files_purged += 1
        return True

    def _purge_artwork(self, target: ArtworkTarget) -> bool:
        stats = ArtworkStats()
        manager = ArtworkManager(self.db, self.settings, self.library_root, stats=stats)
        try:
            ok = manager.purge(target)
        finally:
            manager.close()
        self.stats.files_purged += stats.local_removed
        for message in stats.warnings:
            self.stats.warn(message)
        for message in stats.errors:
            self.stats.error(message)
        self.stats.errors_count += stats.errors_count - len(stats.errors)  # errors beyond the stored messages
        return ok

    def _delete_nfos(self, label: str, key: ItemKey, folders: list[str]) -> bool:
        """Delete the item's NFOs the application created, unless someone changed them.

        Contract:
            Silences:
                - An NFO outside ``folders`` or with a different hash is kept with a warning
                  (and a changed one is marked ``modified`` for good). An NFO that cannot be
                  read hashes as ``None`` and is treated as changed, so a transient read error
                  gives up its ownership permanently.
                - A failed delete -> an error and False; the other NFOs are still processed.
        """
        ok = True
        for record in self.nfo_files.for_item(key):
            path = record.nfo_path
            if record.status != MANAGED or not atomic.is_file(path):
                continue
            if not any(_in_folder(path, f) for f in folders):
                self.stats.warn(f"{label}: {os.path.basename(path)} is not in the item's folder; kept")
                continue
            if file_sha256(path) != record.file_hash:
                self.nfo_files.mark_modified(path)
                self.stats.warn(f"{label}: {os.path.basename(path)} was changed outside the application; kept")
                continue
            try:
                os.remove(atomic.fs_path(path))
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.stats.error(f"{label}: {os.path.basename(path)} could not be deleted: {exc}")
                ok = False
                continue
            self.stats.files_purged += 1
        return ok

    def _remove_empty_dir(self, folder: str) -> None:
        """Remove ``folder`` only when it is empty; anything left in it is kept, with the folder."""
        try:
            if atomic.is_dir(folder) and not os.listdir(atomic.fs_path(folder)):
                os.rmdir(atomic.fs_path(folder))
        except OSError as exc:
            log.debug("Folder %s not removed: %s", folder, exc)
