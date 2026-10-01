"""The one entry point every subsystem uses to create or change NFO files."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from ..filesystem import atomic
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


def update_nfo(path: str, kind: NfoKind, mutate: Mutator, seed: Optional[NfoSeed] = None) -> NfoUpdateResult:
    """Load (or create) the NFO at ``path``, apply ``mutate`` and save if anything changed.

    Raises :class:`~xtreamscraper.nfo.document.NfoParseError` for an existing file that is
    not valid NFO XML: such a file is never overwritten.
    """
    with _lock_for(path):
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
        return NfoUpdateResult(path=path, created=created, written=written)
