"""Persistent synchronization state for Movies, Series and Episodes.

Identity is always the provider's stable IDs (provider, category, stream/series/episode
ID). Titles and paths are stored as attributes so renames can be detected, never used
as keys.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Iterable, Optional

from .db import Database

ACTIVE = "active"
MISSING = "missing"


@dataclass
class MovieRecord:
    provider_id: int
    category_id: str
    stream_id: str
    title: str
    year: Optional[int]
    extension: str
    base_name: str
    folder_name: str
    folder_path: str
    strm_path: str
    url_hash: str
    source_fingerprint: str
    status: str
    first_seen: str
    last_seen: str
    last_updated: str
    last_synced: str


@dataclass
class SeriesRecord:
    provider_id: int
    category_id: str
    series_id: str
    title: str
    year: Optional[int]
    base_name: str
    folder_name: str
    folder_path: str
    last_modified: Optional[str]
    info_fetched_at: Optional[str]
    episode_count: int
    source_fingerprint: str
    status: str
    first_seen: str
    last_seen: str
    last_updated: str
    last_synced: str


@dataclass
class EpisodeRecord:
    provider_id: int
    category_id: str
    series_id: str
    episode_id: str
    season: int
    episode: int
    title: str
    extension: str
    base_name: str
    file_name: str
    strm_path: str
    url_hash: str
    source_fingerprint: str
    status: str
    first_seen: str
    last_seen: str
    last_updated: str
    last_synced: str


def _from_row(cls, row):
    return cls(**{f.name: row[f.name] for f in fields(cls)})


def _upsert(conn, table: str, record, keys: tuple[str, ...]) -> None:
    data = asdict(record)
    columns = ", ".join(data)
    placeholders = ", ".join("?" for _ in data)
    updates = ", ".join(f"{c} = excluded.{c}" for c in data if c not in keys and c != "first_seen")
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET {updates}",
        tuple(data.values()),
    )


class SyncStateRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- movies --------------------------------------------------------------------------
    def movies_in_category(self, provider_id: int, category_id: str) -> dict[str, MovieRecord]:
        rows = self.db.query(
            "SELECT * FROM movies WHERE provider_id = ? AND category_id = ?", (provider_id, category_id)
        )
        return {r["stream_id"]: _from_row(MovieRecord, r) for r in rows}

    def upsert_movies(self, records: Iterable[MovieRecord]) -> None:
        with self.db.transaction() as conn:
            for record in records:
                _upsert(conn, "movies", record, ("provider_id", "category_id", "stream_id"))

    def upsert_movie(self, record: MovieRecord) -> None:
        self.upsert_movies([record])

    def touch_movies(self, provider_id: int, category_id: str, stream_ids: Iterable[str], now: str) -> None:
        with self.db.transaction() as conn:
            conn.executemany(
                "UPDATE movies SET last_seen = ?, last_synced = ?, status = 'active' "
                "WHERE provider_id = ? AND category_id = ? AND stream_id = ?",
                [(now, now, provider_id, category_id, sid) for sid in stream_ids],
            )

    def mark_missing_movies(self, provider_id: int, category_id: str, seen_ids: set[str]) -> int:
        existing = self.db.query(
            "SELECT stream_id FROM movies WHERE provider_id = ? AND category_id = ? AND status = 'active'",
            (provider_id, category_id),
        )
        gone = [r["stream_id"] for r in existing if r["stream_id"] not in seen_ids]
        if gone:
            with self.db.transaction() as conn:
                conn.executemany(
                    "UPDATE movies SET status = 'missing' WHERE provider_id = ? AND category_id = ? AND stream_id = ?",
                    [(provider_id, category_id, sid) for sid in gone],
                )
        return len(gone)

    # -- series --------------------------------------------------------------------------
    def series_in_category(self, provider_id: int, category_id: str) -> dict[str, SeriesRecord]:
        rows = self.db.query(
            "SELECT * FROM series WHERE provider_id = ? AND category_id = ?", (provider_id, category_id)
        )
        return {r["series_id"]: _from_row(SeriesRecord, r) for r in rows}

    def upsert_series(self, record: SeriesRecord) -> None:
        with self.db.transaction() as conn:
            _upsert(conn, "series", record, ("provider_id", "category_id", "series_id"))

    def mark_missing_series(self, provider_id: int, category_id: str, seen_ids: set[str]) -> tuple[int, int]:
        """Mark vanished series (and their episodes) missing. Returns (series, episodes)."""
        existing = self.db.query(
            "SELECT series_id FROM series WHERE provider_id = ? AND category_id = ? AND status = 'active'",
            (provider_id, category_id),
        )
        gone = [r["series_id"] for r in existing if r["series_id"] not in seen_ids]
        episodes = 0
        if gone:
            with self.db.transaction() as conn:
                for sid in gone:
                    conn.execute(
                        "UPDATE series SET status = 'missing' WHERE provider_id = ? AND category_id = ? AND series_id = ?",
                        (provider_id, category_id, sid),
                    )
                    cur = conn.execute(
                        "UPDATE episodes SET status = 'missing' WHERE provider_id = ? AND category_id = ? "
                        "AND series_id = ? AND status = 'active'",
                        (provider_id, category_id, sid),
                    )
                    episodes += cur.rowcount
        return len(gone), episodes

    # -- episodes ------------------------------------------------------------------------
    def episodes_of_series(self, provider_id: int, category_id: str, series_id: str) -> dict[str, EpisodeRecord]:
        rows = self.db.query(
            "SELECT * FROM episodes WHERE provider_id = ? AND category_id = ? AND series_id = ?",
            (provider_id, category_id, series_id),
        )
        return {r["episode_id"]: _from_row(EpisodeRecord, r) for r in rows}

    def upsert_episodes(self, records: Iterable[EpisodeRecord]) -> None:
        with self.db.transaction() as conn:
            for record in records:
                _upsert(conn, "episodes", record, ("provider_id", "category_id", "series_id", "episode_id"))

    def touch_episodes(self, provider_id: int, category_id: str, series_id: str, now: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE episodes SET last_seen = ?, last_synced = ? "
                "WHERE provider_id = ? AND category_id = ? AND series_id = ? AND status = 'active'",
                (now, now, provider_id, category_id, series_id),
            )

    def mark_missing_episodes(self, provider_id: int, category_id: str, series_id: str, seen_ids: set[str]) -> int:
        existing = self.episodes_of_series(provider_id, category_id, series_id)
        gone = [eid for eid, rec in existing.items() if rec.status == ACTIVE and eid not in seen_ids]
        if gone:
            with self.db.transaction() as conn:
                conn.executemany(
                    "UPDATE episodes SET status = 'missing' WHERE provider_id = ? AND category_id = ? "
                    "AND series_id = ? AND episode_id = ?",
                    [(provider_id, category_id, series_id, eid) for eid in gone],
                )
        return len(gone)

    # -- summaries -----------------------------------------------------------------------
    def counts(self, provider_id: int) -> dict[str, int]:
        result = {}
        for table in ("movies", "series", "episodes"):
            for status in (ACTIVE, MISSING):
                row = self.db.query_one(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE provider_id = ? AND status = ?", (provider_id, status)
                )
                result[f"{table}_{status}"] = int(row["n"]) if row else 0
        return result
