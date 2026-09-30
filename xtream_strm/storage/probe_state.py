"""Persistent per-item media probe state."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Optional

from .db import Database

STATUS_NEVER = "never"
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_INVALID_STRM = "invalid_strm"


@dataclass
class ProbeRecord:
    provider_id: int
    content_type: str  # 'movie' | 'episode'
    category_id: str
    item_id: str
    series_id: Optional[str]
    strm_path: str
    url_hash: Optional[str] = None
    last_attempt_at: Optional[str] = None
    last_success_at: Optional[str] = None
    status: str = STATUS_NEVER
    failed_attempts: int = 0
    last_error: Optional[str] = None
    media_info: Optional[str] = None  # JSON produced by probe.models.MediaInfo.to_json()
    media_schema: Optional[int] = None


_KEYS = ("provider_id", "content_type", "category_id", "item_id")


class ProbeStateRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, provider_id: int, content_type: str, category_id: str, item_id: str) -> Optional[ProbeRecord]:
        row = self.db.query_one(
            "SELECT * FROM probe_state WHERE provider_id = ? AND content_type = ? AND category_id = ? AND item_id = ?",
            (provider_id, content_type, category_id, item_id),
        )
        if row is None:
            return None
        return ProbeRecord(**{f.name: row[f.name] for f in fields(ProbeRecord)})

    def all_for_provider(self, provider_id: int) -> dict[tuple[str, str, str], ProbeRecord]:
        rows = self.db.query("SELECT * FROM probe_state WHERE provider_id = ?", (provider_id,))
        result = {}
        for row in rows:
            rec = ProbeRecord(**{f.name: row[f.name] for f in fields(ProbeRecord)})
            result[(rec.content_type, rec.category_id, rec.item_id)] = rec
        return result

    def save(self, record: ProbeRecord) -> None:
        data = asdict(record)
        columns = ", ".join(data)
        placeholders = ", ".join("?" for _ in data)
        updates = ", ".join(f"{c} = excluded.{c}" for c in data if c not in _KEYS)
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO probe_state ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT({', '.join(_KEYS)}) DO UPDATE SET {updates}",
                tuple(data.values()),
            )

    def counts(self, provider_id: int) -> dict[str, int]:
        rows = self.db.query(
            "SELECT status, COUNT(*) AS n FROM probe_state WHERE provider_id = ? GROUP BY status", (provider_id,)
        )
        return {r["status"]: int(r["n"]) for r in rows}
