"""The one entry point every subsystem uses to create or change NFO files."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from ..filesystem import atomic
from ..storage.nfo_files import MANAGED, ItemKey, NfoFileRepository
from ..utils.hashing import file_sha256
from .document import NfoDocument, NfoError
from .paths import NfoKind

log = logging.getLogger(__name__)

Mutator = Callable[[NfoDocument], object]

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    key = atomic.fs_path(path).casefold()
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


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
        someone else: from now on it is theirs, even after the application writes to it again."""
        record = self.repo.get(path)
        if record is None or record.status != MANAGED:
            return
        current = file_sha256(path)
        if current is not None and current != record.file_hash:
            self.repo.mark_modified(path)
            log.info("NFO changed outside the application, it will never be deleted: %s", path)

    def after_write(self, path: str, kind: NfoKind, created: bool) -> None:
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
    """
    with _lock_for(path):
        if owner is not None:
            owner.before_write(path)
        doc = NfoDocument.load_or_create(path, kind)
        created = not doc.existed
        if created and seed is not None:
            seed.apply(doc)
        mutate(doc)
        try:
            written = doc.save(path)
        except NfoError:
            raise
        if written:
            log.debug("NFO %s: %s", "created" if created else "updated", path)
            if owner is not None:
                owner.after_write(path, kind, created)
        return NfoUpdateResult(path=path, created=created, written=written)
