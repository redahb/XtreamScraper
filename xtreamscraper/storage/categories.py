"""Provider categories and the user's per-provider Movie/Series category selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from ..utils.timeutil import now_iso
from .db import Database

MOVIE = "movie"
SERIES = "series"
CONTENT_TYPES = (MOVIE, SERIES)


@dataclass
class CategoryRecord:
    provider_id: int
    content_type: str
    category_id: str
    name: str
    selected: bool
    present: bool
    folder_name: Optional[str]
    #: consecutive successful syncs that returned no items (see the sync engine's empty check)
    empty_syncs: int = 0

    def public_dict(self) -> dict:
        return {
            "category_id": self.category_id,
            "name": self.name,
            "selected": self.selected,
            "present": self.present,
        }


def _row(r) -> CategoryRecord:
    return CategoryRecord(
        provider_id=r["provider_id"],
        content_type=r["content_type"],
        category_id=r["category_id"],
        name=r["name"],
        selected=bool(r["selected"]),
        present=bool(r["present"]),
        folder_name=r["folder_name"],
        empty_syncs=int(r["empty_syncs"] or 0),
    )


class CategoryRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list(self, provider_id: int, content_type: str) -> list[CategoryRecord]:
        rows = self.db.query(
            "SELECT * FROM categories WHERE provider_id = ? AND content_type = ? "
            "ORDER BY present DESC, name COLLATE NOCASE, category_id",
            (provider_id, content_type),
        )
        return [_row(r) for r in rows]

    def selected(self, provider_id: int, content_type: str) -> list[CategoryRecord]:
        return [c for c in self.list(provider_id, content_type) if c.selected]

    def replace_from_provider(self, provider_id: int, content_type: str, categories: Iterable[tuple[str, str]]) -> int:
        """Upsert the provider's current categories, keeping existing selections.

        Categories no longer returned are kept (with ``present = 0``) so a temporary
        provider glitch never silently loses the user's selection.
        """
        now = now_iso()
        seen: set[str] = set()
        with self.db.transaction() as conn:
            for category_id, name in categories:
                if category_id in seen:
                    continue
                seen.add(category_id)
                conn.execute(
                    """
                    INSERT INTO categories(provider_id, content_type, category_id, name, selected, present, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, 0, 1, ?, ?)
                    ON CONFLICT(provider_id, content_type, category_id)
                    DO UPDATE SET name = excluded.name, present = 1, last_seen = excluded.last_seen
                    """,
                    (provider_id, content_type, category_id, name, now, now),
                )
            if seen:
                placeholders = ",".join("?" for _ in seen)
                conn.execute(
                    f"UPDATE categories SET present = 0 WHERE provider_id = ? AND content_type = ? "
                    f"AND category_id NOT IN ({placeholders})",
                    (provider_id, content_type, *seen),
                )
        return len(seen)

    def set_selection(self, provider_id: int, content_type: str, selected_ids: Iterable[str]) -> None:
        wanted = {str(c) for c in selected_ids}
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE categories SET selected = 0 WHERE provider_id = ? AND content_type = ?",
                (provider_id, content_type),
            )
            for category_id in wanted:
                conn.execute(
                    "UPDATE categories SET selected = 1 WHERE provider_id = ? AND content_type = ? AND category_id = ?",
                    (provider_id, content_type, category_id),
                )

    def set_folder_name(self, provider_id: int, content_type: str, category_id: str, folder_name: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE categories SET folder_name = ? WHERE provider_id = ? AND content_type = ? AND category_id = ?",
                (folder_name, provider_id, content_type, category_id),
            )

    def set_empty_syncs(self, provider_id: int, content_type: str, category_id: str, count: int) -> None:
        self.db.execute(
            "UPDATE categories SET empty_syncs = ? WHERE provider_id = ? AND content_type = ? AND category_id = ?",
            (count, provider_id, content_type, category_id),
        )
