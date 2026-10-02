"""Ownership of NFO files the application created.

An NFO is the application's only when a row here says so, and a row is only ever written
at the moment the application creates the file (see :func:`..nfo.service.update_nfo`).
Editing tags inside an existing NFO never establishes ownership, and ownership is never
inferred from a file name or its XML. A managed NFO whose content no longer matches the
hash recorded at the application's last write was changed by someone else: it is marked
``modified`` for good and is never deleted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from ..utils.timeutil import now_iso
from .db import Database

#: (provider_id, item_kind, category_id, item_id), the same key the artwork tables use.
ItemKey = tuple[int, str, str, str]

MANAGED = "managed"
MODIFIED = "modified"


def path_key(path: str) -> str:
    return os.path.abspath(path)


@dataclass
class NfoFileRecord:
    nfo_path: str
    nfo_kind: str
    provider_id: int
    item_kind: str
    category_id: str
    item_id: str
    file_hash: str
    status: str
    created_at: str
    updated_at: str

    @property
    def key(self) -> ItemKey:
        return (self.provider_id, self.item_kind, self.category_id, self.item_id)


def _record(row) -> NfoFileRecord:
    return NfoFileRecord(**{k: row[k] for k in row.keys()})


class NfoFileRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, path: str) -> Optional[NfoFileRecord]:
        row = self.db.query_one("SELECT * FROM nfo_files WHERE nfo_path = ?", (path_key(path),))
        return _record(row) if row else None

    def register(self, path: str, nfo_kind: str, key: ItemKey, file_hash: str) -> None:
        """The application just created ``path``: it owns it from now on."""
        now = now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO nfo_files(nfo_path, nfo_kind, provider_id, item_kind, category_id, item_id, file_hash, "
                "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(nfo_path) DO UPDATE SET nfo_kind = excluded.nfo_kind, provider_id = excluded.provider_id, "
                "item_kind = excluded.item_kind, category_id = excluded.category_id, item_id = excluded.item_id, "
                "file_hash = excluded.file_hash, status = excluded.status, created_at = excluded.created_at, "
                "updated_at = excluded.updated_at",
                (path_key(path), nfo_kind, *key, file_hash, MANAGED, now, now),
            )

    def set_hash(self, path: str, file_hash: str) -> None:
        self.db.execute("UPDATE nfo_files SET file_hash = ?, updated_at = ? WHERE nfo_path = ? AND status = ?",
                        (file_hash, now_iso(), path_key(path), MANAGED))

    def mark_modified(self, path: str) -> None:
        self.db.execute("UPDATE nfo_files SET status = ?, updated_at = ? WHERE nfo_path = ?",
                        (MODIFIED, now_iso(), path_key(path)))

    def for_item(self, key: ItemKey) -> list[NfoFileRecord]:
        rows = self.db.query(
            "SELECT * FROM nfo_files WHERE provider_id = ? AND item_kind = ? AND category_id = ? AND item_id = ? "
            "ORDER BY nfo_path", key)
        return [_record(r) for r in rows]

    def forget(self, path: str) -> None:
        self.db.execute("DELETE FROM nfo_files WHERE nfo_path = ?", (path_key(path),))

    def move(self, old_path: str, new_path: str) -> None:
        """A sidecar rename moved an NFO (episode renamed)."""
        old, new = path_key(old_path), path_key(new_path)
        if old == new:
            return
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM nfo_files WHERE nfo_path = ?", (new,))
            conn.execute("UPDATE nfo_files SET nfo_path = ? WHERE nfo_path = ?", (new, old))

    def move_folder(self, provider_id: int, old_folder: str, new_folder: str) -> None:
        """An item folder was moved (movie or series renamed): every NFO below it moves along."""
        old, new = path_key(old_folder).rstrip(os.sep), path_key(new_folder).rstrip(os.sep)
        if old == new:
            return
        rows = self.db.query("SELECT nfo_path FROM nfo_files WHERE provider_id = ?", (provider_id,))
        moves = [(new + r["nfo_path"][len(old):], r["nfo_path"]) for r in rows
                 if r["nfo_path"].startswith(old + os.sep)]
        if moves:
            with self.db.transaction() as conn:
                for target, source in moves:
                    conn.execute("DELETE FROM nfo_files WHERE nfo_path = ?", (target,))
                    conn.execute("UPDATE nfo_files SET nfo_path = ? WHERE nfo_path = ?", (target, source))
