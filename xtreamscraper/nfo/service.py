"""The one entry point every subsystem uses to create or change NFO files."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from ..filesystem import atomic
from ..storage.nfo_files import MANAGED, ItemKey, NfoFileRepository
from ..utils.hashing import file_sha256
from .document import NfoDocument
from .paths import NfoKind

log = logging.getLogger(__name__)

Mutator = Callable[[NfoDocument], object]

@dataclass
class _PathLock:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


# One lock per NFO path while anyone uses it; entries are dropped when the last user leaves,
# so the table stays as small as the number of concurrent writes.
_locks: dict[str, _PathLock] = {}
_locks_guard = threading.Lock()


@contextmanager
def _locked(path: str) -> Iterator[None]:
    key = atomic.fs_path(path).casefold()
    with _locks_guard:
        entry = _locks.get(key)
        if entry is None:
            entry = _locks[key] = _PathLock()
        entry.users += 1
    try:
        with entry.lock:
            yield
    finally:
        with _locks_guard:
            entry.users -= 1
            if entry.users == 0:
                _locks.pop(key, None)


@dataclass
class NfoUpdateResult:
    path: str
    created: bool
    written: bool


@dataclass
class NfoSeed:
    """Basic identifying fields written only when a new NFO is created."""

    title: Optional[str] = None
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    show_title: Optional[str] = None

    def apply(self, doc: NfoDocument) -> None:
        doc.set_text("title", self.title or None)
        if doc.kind is NfoKind.EPISODE:
            doc.set_text("showtitle", self.show_title or None)
            doc.set_text("season", self.season)
            doc.set_text("episode", self.episode)
        elif doc.kind is NfoKind.SEASON:
            doc.set_text("seasonnumber", self.season)
        elif self.year:
            doc.set_text("year", self.year)


@dataclass
class NfoOwner:
    """The library item an NFO write is for, so the NFOs the application creates are recorded
    as its own (see :mod:`..storage.nfo_files`)."""

    repo: NfoFileRepository
    key: ItemKey

    def before_write(self, path: str) -> None:
        """A managed NFO whose content changed since the application's last write was edited by
        someone else: from now on it is theirs, even after the application writes to it again.

        Contract:
            Raises:
                sqlite3.Error: From the repository (not caught).
            Silences:
                - An unreadable or missing file (``file_sha256`` gives ``None``) is not marked
                  modified; the record stays managed.
        """
        record = self.repo.get(path)
        if record is None or record.status != MANAGED:
            return
        current = file_sha256(path)
        if current is not None and current != record.file_hash:
            self.repo.mark_modified(path)
            log.info("NFO changed outside the application, it will never be deleted: %s", path)

    def after_write(self, path: str, kind: NfoKind, created: bool) -> None:
        """Record a created NFO as owned, or refresh the stored hash after an update.

        Contract:
            Raises:
                sqlite3.Error: From the repository. The file is already written then, and
                    its ownership record is missing or stale.
            Silences:
                - When the written file cannot be read back (``file_sha256`` is ``None``)
                  nothing is recorded: a created NFO is then not owned by the application.
        """
        digest = file_sha256(path)
        if digest is None:
            return
        if created:
            self.repo.register(path, kind.value, self.key, digest)
        else:
            self.repo.set_hash(path, digest)  # only touches managed (unmodified) records


def update_nfo(path: str, kind: NfoKind, mutate: Mutator, seed: Optional[NfoSeed] = None,
               owner: Optional[NfoOwner] = None) -> NfoUpdateResult:
    """Load (or create) the NFO at ``path``, apply ``mutate`` and save if anything changed.

    Raises :class:`~xtreamscraper.nfo.document.NfoParseError` for an existing file that is
    not valid NFO XML: such a file is never overwritten. With an ``owner``, an NFO this call
    creates is recorded as the application's, and later writes keep its recorded hash current.

    Contract:
        Preconditions:
            - ``mutate`` edits the document in place; its return value is ignored. It runs
              under a per-path lock (case-insensitive key), so it must not call
              ``update_nfo`` for the same path (the lock is not re-entrant: deadlock).
            - The lock only serialises writers in this process.
        Raises:
            NfoParseError: The existing file does not decode or is not NFO XML of ``kind``.
            NfoError: The file cannot be read, or it appeared while a new one was created.
            OSError: The atomic write failed (the file is unchanged).
            Exception: Anything ``mutate`` raises (e.g. ``ValueError`` from
                ``apply_media_info`` for a tvshow/season NFO); nothing is written then.
            sqlite3.Error: From ``owner``; after a successful write this leaves the
                ownership record out of date.
    """
    with _locked(path):
        if owner is not None:
            owner.before_write(path)
        doc = NfoDocument.load_or_create(path, kind)
        created = not doc.existed
        if created and seed is not None:
            seed.apply(doc)
        mutate(doc)
        written = doc.save(path)
        if written:
            log.debug("NFO %s: %s", "created" if created else "updated", path)
            if owner is not None:
                owner.after_write(path, kind, created)
        return NfoUpdateResult(path=path, created=created, written=written)
