"""Persistent artwork state: selection per slot and ownership of files and NFO references.

Ownership is only ever established by these rows (never inferred from a file name): a
local file is the application's when an ``artwork_files`` row with its path and hash
exists, a remote NFO reference when an ``artwork_nfo_refs`` row with its NFO path,
location and URL exists. Items are keyed by stable Xtream IDs, not paths; stored paths are
updated when the library moves.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional, TypedDict, TypeVar, Union

from ..utils.timeutil import now_iso
from .db import Database

#: (provider_id, item_kind, category_id, item_id)
ItemKey = tuple[int, str, str, str]

# slot status values
PENDING = "pending"
OK = "ok"
EXTERNAL = "external"  # unmanaged artwork already present: kept, nothing added
KEPT = "kept"  # a newer candidate exists but the existing file is kept (Keep existing)
MODIFIED = "modified"  # a managed file was changed outside the application: protected
DISABLED = "disabled"  # artwork mode or this artwork type is off
ERROR = "error"

# file status values
MANAGED = "managed"
MODIFIED_EXTERNALLY = "modified"


@dataclass
class SlotRecord:
    id: int
    provider_id: int
    item_kind: str
    category_id: str
    item_id: str
    artwork_type: str
    source_plugin: Optional[str]
    source_remote_id: Optional[str]
    source_url: str
    language: Optional[str]
    width: Optional[int]
    height: Optional[int]
    selected_at: str
    mode: Optional[str]
    status: str
    message: Optional[str]
    updated_at: str

    @property
    def key(self) -> ItemKey:
        return (self.provider_id, self.item_kind, self.category_id, self.item_id)


@dataclass
class FileRecord:
    id: int
    slot_id: int
    role: str
    local_path: str
    file_hash: str
    size: int
    source_url: str
    source_plugin: Optional[str]
    status: str
    downloaded_at: str
    updated_at: str


@dataclass
class RefRecord:
    id: int
    slot_id: int
    nfo_path: str
    nfo_kind: str
    location: str
    url: str
    source_plugin: Optional[str]
    written_at: str


_Record = TypeVar("_Record", bound=Union[SlotRecord, FileRecord, RefRecord])


class ArtworkCounts(TypedDict):
    """Artwork overview figures for the WebUI."""

    slots: dict[str, int]  # slot status -> count
    slot_total: int
    managed_files: int
    modified_files: int
    nfo_refs: int


def _build(cls: type[_Record], row: sqlite3.Row) -> _Record:
    return cls(**{k: row[k] for k in row.keys()})


class ArtworkRepository:
    db: Database

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- items -------------------------------------------------------------------------------------
    def mark_evaluated(self, key: ItemKey) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO artwork_items(provider_id, item_kind, category_id, item_id, evaluated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider_id, item_kind, category_id, item_id) "
                "DO UPDATE SET evaluated_at = excluded.evaluated_at",
                (*key, now_iso()),
            )

    def was_evaluated(self, key: ItemKey) -> bool:
        return self.db.query_one(
            "SELECT 1 FROM artwork_items WHERE provider_id = ? AND item_kind = ? AND category_id = ? AND item_id = ?",
            key) is not None

    def item_keys(self, provider_id: int) -> list[ItemKey]:
        rows = self.db.query(
            "SELECT DISTINCT provider_id, item_kind, category_id, item_id FROM artwork_slots WHERE provider_id = ?",
            (provider_id,))
        return [(r["provider_id"], r["item_kind"], r["category_id"], r["item_id"]) for r in rows]

    # -- slots -------------------------------------------------------------------------------------
    def slots(self, key: ItemKey) -> list[SlotRecord]:
        rows = self.db.query(
            "SELECT * FROM artwork_slots WHERE provider_id = ? AND item_kind = ? AND category_id = ? AND item_id = ? "
            "ORDER BY artwork_type", key)
        return [_build(SlotRecord, r) for r in rows]

    def slots_for_provider(self, provider_id: int) -> dict[ItemKey, list[SlotRecord]]:
        result: dict[ItemKey, list[SlotRecord]] = {}
        for r in self.db.query("SELECT * FROM artwork_slots WHERE provider_id = ? ORDER BY id", (provider_id,)):
            slot = _build(SlotRecord, r)
            result.setdefault(slot.key, []).append(slot)
        return result

    def get_slot(self, slot_id: int) -> Optional[SlotRecord]:
        row = self.db.query_one("SELECT * FROM artwork_slots WHERE id = ?", (slot_id,))
        return _build(SlotRecord, row) if row else None

    def select(self, key: ItemKey, artwork_type: str, url: str, source_plugin: Optional[str],
               source_remote_id: Optional[str], language: Optional[str] = None, width: Optional[int] = None,
               height: Optional[int] = None) -> SlotRecord:
        """Persist the selected candidate for a slot (``selected_at`` changes only with the URL/source).

        A new slot starts ``pending``; an existing slot keeps its ``status``, ``mode`` and
        ``message`` (the caller updates them through :meth:`set_status`).
        """
        now = now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO artwork_slots(provider_id, item_kind, category_id, item_id, artwork_type, source_plugin,
                    source_remote_id, source_url, language, width, height, selected_at, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(provider_id, item_kind, category_id, item_id, artwork_type) DO UPDATE SET
                    selected_at = CASE WHEN artwork_slots.source_url = excluded.source_url
                                        AND COALESCE(artwork_slots.source_plugin, '') = COALESCE(excluded.source_plugin, '')
                                       THEN artwork_slots.selected_at ELSE excluded.selected_at END,
                    source_plugin = excluded.source_plugin, source_remote_id = excluded.source_remote_id,
                    source_url = excluded.source_url, language = excluded.language, width = excluded.width,
                    height = excluded.height, updated_at = excluded.updated_at
                """,
                (*key, artwork_type, source_plugin, source_remote_id, url, language, width, height, now, now),
            )
            row = conn.execute(
                "SELECT * FROM artwork_slots WHERE provider_id = ? AND item_kind = ? AND category_id = ? "
                "AND item_id = ? AND artwork_type = ?", (*key, artwork_type)).fetchone()
        return _build(SlotRecord, row)

    def set_status(self, slot_id: int, status: str, message: Optional[str] = None, mode: Optional[str] = None,
                   keep_mode: bool = False) -> None:
        """Set the slot's status and message, and its mode unless ``keep_mode``.

        Contract:
            Preconditions:
                - Without ``keep_mode=True`` the stored mode becomes ``mode``, so leaving
                  ``mode`` out clears it to ``NULL``.
                - ``message`` replaces the stored one (``None`` clears it).
        """
        with self.db.transaction() as conn:
            if keep_mode:
                conn.execute("UPDATE artwork_slots SET status = ?, message = ?, updated_at = ? WHERE id = ?",
                             (status, message, now_iso(), slot_id))
            else:
                conn.execute("UPDATE artwork_slots SET status = ?, message = ?, mode = ?, updated_at = ? WHERE id = ?",
                             (status, message, mode, now_iso(), slot_id))

    # -- managed files -----------------------------------------------------------------------------
    def files(self, slot_id: int) -> list[FileRecord]:
        """The slot's managed files, ``primary`` before ``alias`` (``role DESC``), then by ID."""
        rows = self.db.query("SELECT * FROM artwork_files WHERE slot_id = ? ORDER BY role DESC, id", (slot_id,))
        return [_build(FileRecord, r) for r in rows]

    def record_file(self, slot_id: int, role: str, path: str, file_hash: str, size: int, source_url: str,
                    source_plugin: Optional[str]) -> None:
        """The application wrote ``path`` for this slot: it owns it, as ``managed``.

        Contract:
            Preconditions:
                - ``path`` was just written by the application with content hashing to
                  ``file_hash``. An existing row for the same slot and path is overwritten and
                  set back to ``managed``, also when it was ``modified``.
                - ``slot_id`` exists (foreign key, else ``sqlite3.IntegrityError``).
        """
        now = now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO artwork_files(slot_id, role, local_path, file_hash, size, source_url, source_plugin,
                    status, downloaded_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'managed', ?, ?)
                ON CONFLICT(slot_id, local_path) DO UPDATE SET role = excluded.role, file_hash = excluded.file_hash,
                    size = excluded.size, source_url = excluded.source_url, source_plugin = excluded.source_plugin,
                    status = 'managed', downloaded_at = excluded.downloaded_at, updated_at = excluded.updated_at
                """,
                (slot_id, role, path, file_hash, size, source_url, source_plugin, now, now),
            )

    def move_file(self, file_id: int, new_path: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE artwork_files SET local_path = ?, updated_at = ? WHERE id = ?",
                         (new_path, now_iso(), file_id))

    def set_file_status(self, file_id: int, status: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE artwork_files SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), file_id))

    def forget_file(self, file_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM artwork_files WHERE id = ?", (file_id,))

    # -- managed NFO references --------------------------------------------------------------------
    def refs(self, slot_id: int) -> list[RefRecord]:
        rows = self.db.query("SELECT * FROM artwork_nfo_refs WHERE slot_id = ? ORDER BY id", (slot_id,))
        return [_build(RefRecord, r) for r in rows]

    def refs_at(self, nfo_path: str, location: str) -> list[RefRecord]:
        """Owned NFO references at one NFO location, any slot. ``nfo_path`` is compared exactly
        (no normalisation), so it must be spelled as it was recorded."""
        rows = self.db.query("SELECT * FROM artwork_nfo_refs WHERE nfo_path = ? AND location = ?", (nfo_path, location))
        return [_build(RefRecord, r) for r in rows]

    def record_ref(self, slot_id: int, nfo_path: str, nfo_kind: str, location: str, url: str,
                   source_plugin: Optional[str]) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO artwork_nfo_refs(slot_id, nfo_path, nfo_kind, location, url, source_plugin, written_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(slot_id, nfo_path, location, url) "
                "DO UPDATE SET written_at = excluded.written_at, source_plugin = excluded.source_plugin",
                (slot_id, nfo_path, nfo_kind, location, url, source_plugin, now_iso()),
            )

    def move_ref(self, ref_id: int, new_nfo_path: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE artwork_nfo_refs SET nfo_path = ? WHERE id = ?", (new_nfo_path, ref_id))

    def delete_slot(self, slot_id: int) -> None:
        """Forget a slot that no longer owns any file or NFO reference."""
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM artwork_slots WHERE id = ?", (slot_id,))

    def forget_ref(self, ref_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM artwork_nfo_refs WHERE id = ?", (ref_id,))

    # -- overview ----------------------------------------------------------------------------------
    def counts(self) -> ArtworkCounts:
        """Slot counts per status, managed and modified file counts, and owned NFO references."""
        slots = {r["status"]: r["n"] for r in self.db.query(
            "SELECT status, COUNT(*) AS n FROM artwork_slots GROUP BY status")}
        files = {r["status"]: r["n"] for r in self.db.query(
            "SELECT status, COUNT(*) AS n FROM artwork_files GROUP BY status")}
        row = self.db.query_one("SELECT COUNT(*) AS n FROM artwork_nfo_refs")
        assert row is not None  # an aggregate query always returns one row
        refs = row["n"]
        return {"slots": slots, "slot_total": sum(slots.values()), "managed_files": files.get(MANAGED, 0),
                "modified_files": files.get(MODIFIED_EXTERNALLY, 0), "nfo_refs": refs}
