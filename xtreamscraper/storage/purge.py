"""Removal of a permanently purged item's application state, in one transaction per item.

Only called after the item's managed files were dealt with (see
:mod:`..sync.lifecycle`): until then every row needed to retry the clean-up stays. Job and
history tables are never touched; history stays historical.
"""

from __future__ import annotations

from .db import Database


def _forget_artwork(conn, provider_id: int, item_kind: str, category_id: str, item_ids: list[str]) -> None:
    for item_id in item_ids:
        key = (provider_id, item_kind, category_id, item_id)
        # artwork_files / artwork_nfo_refs go with their slot (ON DELETE CASCADE)
        conn.execute("DELETE FROM artwork_slots WHERE provider_id = ? AND item_kind = ? AND category_id = ? "
                     "AND item_id = ?", key)
        conn.execute("DELETE FROM artwork_items WHERE provider_id = ? AND item_kind = ? AND category_id = ? "
                     "AND item_id = ?", key)
        conn.execute("DELETE FROM scraper_bindings WHERE provider_id = ? AND item_kind = ? AND category_id = ? "
                     "AND item_id = ?", key)
        conn.execute("DELETE FROM nfo_files WHERE provider_id = ? AND item_kind = ? AND category_id = ? "
                     "AND item_id = ?", key)


class PurgeRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def season_ids(self, provider_id: int, category_id: str, series_id: str) -> list[str]:
        """Season item IDs (``<series_id>/<season>``) the series has any state for."""
        prefix = f"{series_id}/"
        ids: set[str] = set()
        for table in ("artwork_slots", "artwork_items", "scraper_bindings", "nfo_files"):
            rows = self.db.query(
                f"SELECT DISTINCT item_id FROM {table} WHERE provider_id = ? AND item_kind = 'season' "
                "AND category_id = ? AND substr(item_id, 1, ?) = ?",
                (provider_id, category_id, len(prefix), prefix),
            )
            ids.update(r["item_id"] for r in rows)
        return sorted(ids)

    def purge_movie(self, provider_id: int, category_id: str, stream_id: str) -> None:
        with self.db.transaction() as conn:
            _forget_artwork(conn, provider_id, "movie", category_id, [stream_id])
            conn.execute("DELETE FROM probe_state WHERE provider_id = ? AND content_type = 'movie' "
                         "AND category_id = ? AND item_id = ?", (provider_id, category_id, stream_id))
            conn.execute("DELETE FROM movies WHERE provider_id = ? AND category_id = ? AND stream_id = ?",
                         (provider_id, category_id, stream_id))

    def purge_episodes(self, provider_id: int, category_id: str, series_id: str, episode_ids: list[str]) -> None:
        with self.db.transaction() as conn:
            _forget_artwork(conn, provider_id, "episode", category_id, episode_ids)
            for episode_id in episode_ids:
                conn.execute("DELETE FROM probe_state WHERE provider_id = ? AND content_type = 'episode' "
                             "AND category_id = ? AND item_id = ?", (provider_id, category_id, episode_id))
                conn.execute("DELETE FROM episodes WHERE provider_id = ? AND category_id = ? AND series_id = ? "
                             "AND episode_id = ?", (provider_id, category_id, series_id, episode_id))

    def purge_series(self, provider_id: int, category_id: str, series_id: str) -> None:
        """The series, its seasons and every episode it still has."""
        episode_ids = [r["episode_id"] for r in self.db.query(
            "SELECT episode_id FROM episodes WHERE provider_id = ? AND category_id = ? AND series_id = ?",
            (provider_id, category_id, series_id))]
        season_ids = self.season_ids(provider_id, category_id, series_id)
        with self.db.transaction() as conn:
            self.purge_episodes(provider_id, category_id, series_id, episode_ids)  # joins this transaction
            _forget_artwork(conn, provider_id, "season", category_id, season_ids)
            _forget_artwork(conn, provider_id, "series", category_id, [series_id])
            conn.execute("DELETE FROM series WHERE provider_id = ? AND category_id = ? AND series_id = ?",
                         (provider_id, category_id, series_id))
