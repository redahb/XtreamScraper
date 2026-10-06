"""Library directory layout and deterministic collision handling.

Layout::

    <target>/<provider>/Movies/<category>/<Movie (Year)>/<Movie (Year)>.strm
    <target>/<provider>/Series/<category>/<Series (Year)>/Season 01/<Series> S01E01.strm

Movies and Series always live under separate roots, so equally named Movie and Series
categories can never mix.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable, Sequence

from . import atomic
from .sanitize import sanitize_component

log = logging.getLogger(__name__)

MOVIES_DIR = "Movies"
SERIES_DIR = "Series"


class LibraryLayout:
    """Where one provider's library lives: ``<target_root>/<provider_folder>/Movies|Series/...``."""

    target_root: str
    provider_folder: str

    def __init__(self, target_root: str, provider_folder: str) -> None:
        self.target_root = os.path.abspath(target_root)
        self.provider_folder = provider_folder

    @property
    def provider_dir(self) -> str:
        return os.path.join(self.target_root, self.provider_folder)

    def content_root(self, content_type: str) -> str:
        return os.path.join(self.provider_dir, MOVIES_DIR if content_type == "movie" else SERIES_DIR)

    def category_dir(self, content_type: str, category_folder: str) -> str:
        return os.path.join(self.content_root(content_type), category_folder)


# -- collision handling ------------------------------------------------------------------------
def with_suffix(base: str, suffix: str, max_length: int) -> str:
    """``base`` + ``suffix``, shortening ``base`` (not the suffix) to respect ``max_length``.

    Contract:
        Preconditions:
            - ``base`` should already be a sanitized component; a ``:`` in it widens the
              result (``": "`` becomes ``" - "``).

        Silences:
            - The suffix is never shortened, and ``base`` keeps at least 8 characters, so
              the result can be longer than ``max_length``: up to
              ``max(8, max_length - len(suffix)) + len(suffix)``. A long ID in the suffix
              (``[xid-<64 characters>]``) therefore exceeds the setting.
            - An empty ``base`` gives the suffix alone with its leading space stripped
              (``[xid-1]``).
    """
    room = max(8, max_length - len(suffix))
    return sanitize_component(base[:room].rstrip(" ._-") + suffix, max_length=max_length + len(suffix))


def id_suffix(item_id: str) -> str:
    return f" [xid-{item_id}]"


class NameAllocator:
    """Assigns unique, case-insensitive names inside one directory.

    Names already recorded in the database are claimed first, so an item keeps its name
    across runs; new items then take the first free candidate. Candidates always end with
    an ID-based name, which is unique by construction, so allocation cannot fail.
    """

    def __init__(self) -> None:
        self._owners: dict[str, str] = {}

    def claim(self, name: str, owner: str) -> bool:
        key = name.casefold()
        current = self._owners.get(key)
        if current is None or current == owner:
            self._owners[key] = owner
            return True
        return False

    def owner_of(self, name: str) -> str | None:
        return self._owners.get(name.casefold())

    def allocate(
        self,
        owner: str,
        candidates: Sequence[str],
        is_foreign: Callable[[str], bool] | None = None,
    ) -> str:
        """Claim and return the first candidate that is free for ``owner``.

        A candidate is free when nobody holds it (case-insensitively) or ``owner`` already
        does. A name nobody holds is also skipped when ``is_foreign(name)`` says something
        outside the database occupies it.

        Contract:
            Preconditions:
                - The last candidate should be unique by construction (ID-suffixed), so one
                  candidate is always free.

            Silences:
                - Empty candidates are skipped.
                - When every candidate is taken, ``"<last candidate> (2)"``, ``(3)``, ... is
                  claimed instead (``owner`` when there are no candidates). That fallback
                  name is not sanitized, not length-capped and not checked with
                  ``is_foreign``.
        """
        for candidate in candidates:
            if not candidate:
                continue
            key = candidate.casefold()
            current = self._owners.get(key)
            if current is not None and current != owner:
                continue
            if current is None and is_foreign is not None and is_foreign(candidate):
                continue
            self._owners[key] = owner
            return candidate
        # Unreachable with an ID-suffixed candidate, but never loop or overwrite.
        counter = 2
        base = candidates[-1] if candidates else owner
        while True:
            name = f"{base} ({counter})"
            if self.claim(name, owner):
                return name
            counter += 1


def dedupe_candidates(values: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            result.append(value)
    return result


def sidecar_names(directory: str, stem: str) -> list[str]:
    """Files in ``directory`` belonging to ``stem`` (``stem.strm``, ``stem.nfo``, ``stem-thumb.jpg``).

    A name belongs to ``stem`` when it starts with ``stem`` (case-insensitively) directly
    followed by ``.`` or ``-``; ``stem - x.strm`` does not.

    Contract:
        Silences:
            - An ``OSError`` from listing the folder (missing, not a folder, no access)
              returns ``[]``, the same answer as a folder without sidecars.
    """
    try:
        names = os.listdir(atomic.fs_path(directory))
    except OSError:
        return []
    folded_stem = stem.casefold()
    size = len(stem)
    result: list[str] = []
    for name in names:
        # Compare the stem-length prefix itself: folding the whole name can change its length
        # ("STRASSE" folds like "Straße"), which would claim another stem's files.
        # "Show S01E01.strm" must not claim "Show S01E01 - x.strm" style siblings of another stem.
        if name[:size].casefold() == folded_stem and name[size:size + 1] in (".", "-"):
            result.append(name)
    return result


def rename_sidecars(directory: str, old_stem: str, new_stem: str, target_dir: str | None = None) -> int:
    """Move ``old_stem.*`` files to ``new_stem.*`` (optionally into ``target_dir``). Never overwrites.

    Returns the number of files moved. The part after the stem keeps its original case.

    Contract:
        Silences:
            - A file whose target name exists is left in place and not counted.
            - An ``OSError`` while moving one file is logged as a warning, that file is
              skipped and the others are still moved; the count is the only signal.
            - An unreadable ``directory`` moves nothing and returns 0 (see ``sidecar_names``).
    """
    target_dir = target_dir or directory
    moved = 0
    for name in sidecar_names(directory, old_stem):
        new_name = new_stem + name[len(old_stem):]
        try:
            if atomic.move(os.path.join(directory, name), os.path.join(target_dir, new_name)):
                moved += 1
        except OSError as exc:
            log.warning("Could not move %s to %s: %s", name, new_name, exc)
    return moved


def provider_folder_names(providers: Iterable[tuple[int, str, str]], max_length: int) -> dict[int, str]:
    """Folder name per provider ID; ``(id, name, target_root)`` tuples.

    Providers sharing a target folder and a (sanitised, case-insensitive) name are told
    apart with an ID suffix; the lowest ID keeps the plain name.

    Contract:
        Preconditions:
            - Provider IDs are unique. ``target`` paths are grouped by
              ``normcase(abspath(...))``, so an empty or relative target depends on the
              current directory.
    """
    result: dict[int, str] = {}
    allocators: dict[str, NameAllocator] = {}
    for provider_id, name, target in sorted(providers, key=lambda p: p[0]):
        allocator = allocators.setdefault(os.path.normcase(os.path.abspath(target or ".")), NameAllocator())
        base = sanitize_component(name, max_length=max_length)
        result[provider_id] = allocator.allocate(str(provider_id), [base, with_suffix(base, f" [p{provider_id}]", max_length)])
    return result
