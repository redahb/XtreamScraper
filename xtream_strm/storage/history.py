"""Synchronization and probe job history (user-visible statistics), with retention.

This is separate from the synchronization state: deleting history never affects what
the next sync considers new or unchanged.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, Optional

from ..utils.timeutil import to_iso, utcnow
from .db import Database

log = logging.getLogger(__name__)

MAX_DETAIL_MESSAGES = 200  # warnings/errors kept per job
DETAILED_JOBS_PER_PROVIDER = 20  # older jobs keep counters but lose message lists


def _decode(row) -> dict[str, Any]:
    data = dict(row)
    for key in ("stats", "warnings", "errors"):
        try:
            data[key] = json.loads(data.get(key) or ("{}" if key == "stats" else "[]"))
        except ValueError:
            data[key] = {} if key == "stats" else []
    return data


class _HistoryBase:
    table = ""
    summary_columns: tuple[str, ...] = ()

    def __init__(self, db: Database) -> None:
        self.db = db

    def start(self, job_id: str, provider_id: Optional[int], provider_name: str, started_at: str, **extra) -> int:
        columns = ["job_id", "provider_id", "provider_name", "status", "started_at", *extra.keys()]
        values = [job_id, provider_id, provider_name, "running", started_at, *extra.values()]
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"INSERT INTO {self.table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in values)})",
                values,
            )
            return int(cur.lastrowid)

    def finish(
        self,
        row_id: int,
        status: str,
        finished_at: str,
        duration: float,
        stats: dict[str, Any],
        warnings: list[str],
        errors: list[str],
        summary: dict[str, int],
    ) -> None:
        assignments = {
            "status": status,
            "finished_at": finished_at,
            "duration_seconds": duration,
            "stats": json.dumps(stats),
            "warnings": json.dumps(warnings[:MAX_DETAIL_MESSAGES]),
            "errors": json.dumps(errors[:MAX_DETAIL_MESSAGES]),
            "warning_count": len(warnings),
            "error_count": len(errors),
            **{k: int(v) for k, v in summary.items() if k in self.summary_columns},
        }
        sql = ", ".join(f"{k} = ?" for k in assignments)
        with self.db.transaction() as conn:
            conn.execute(f"UPDATE {self.table} SET {sql} WHERE id = ?", (*assignments.values(), row_id))

    def list(self, provider_id: Optional[int] = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        columns = (
            "id, job_id, provider_id, provider_name, status, started_at, finished_at, duration_seconds, "
            "warning_count, error_count, " + ", ".join(self.summary_columns) + self._extra_list_columns()
        )
        if provider_id is None:
            rows = self.db.query(
                f"SELECT {columns} FROM {self.table} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        else:
            rows = self.db.query(
                f"SELECT {columns} FROM {self.table} WHERE provider_id = ? "
                "ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                (provider_id, limit, offset),
            )
        return [dict(r) for r in rows]

    def _extra_list_columns(self) -> str:
        return ""

    def get(self, row_id: int) -> Optional[dict[str, Any]]:
        row = self.db.query_one(f"SELECT * FROM {self.table} WHERE id = ?", (row_id,))
        return _decode(row) if row else None

    def latest_per_provider(self) -> dict[int, dict[str, Any]]:
        rows = self.db.query(
            f"""
            SELECT t.* FROM {self.table} t
            JOIN (SELECT provider_id, MAX(id) AS max_id FROM {self.table}
                  WHERE provider_id IS NOT NULL GROUP BY provider_id) m ON t.id = m.max_id
            """
        )
        return {r["provider_id"]: _decode(r) for r in rows}

    def mark_interrupted(self) -> int:
        """Jobs still 'running' at startup were interrupted by a shutdown/crash."""
        with self.db.transaction() as conn:
            cur = conn.execute(f"UPDATE {self.table} SET status = 'interrupted' WHERE status = 'running'")
            return cur.rowcount

    def apply_retention(self, keep_per_provider: int, max_age_days: int) -> int:
        """Bound history size.

        * Jobs older than ``max_age_days`` are deleted, except each provider's latest job.
        * Only the newest ``keep_per_provider`` jobs per provider are kept at all.
        * Beyond the newest :data:`DETAILED_JOBS_PER_PROVIDER`, message lists are pruned
          (counters are kept, so aggregate numbers stay visible).
        """
        cutoff = to_iso(utcnow() - timedelta(days=max_age_days))
        removed = 0
        with self.db.transaction() as conn:
            latest_ids = [
                r["max_id"]
                for r in conn.execute(
                    f"SELECT MAX(id) AS max_id FROM {self.table} GROUP BY COALESCE(provider_id, -1)"
                ).fetchall()
            ]
            keep = ",".join(str(int(i)) for i in latest_ids) or "-1"
            cur = conn.execute(
                f"DELETE FROM {self.table} WHERE started_at < ? AND id NOT IN ({keep}) AND status != 'running'",
                (cutoff,),
            )
            removed += cur.rowcount
            groups = conn.execute(f"SELECT DISTINCT COALESCE(provider_id, -1) AS pid FROM {self.table}").fetchall()
            for group in groups:
                pid = group["pid"]
                ids = [
                    r["id"]
                    for r in conn.execute(
                        f"SELECT id FROM {self.table} WHERE COALESCE(provider_id, -1) = ? ORDER BY id DESC", (pid,)
                    ).fetchall()
                ]
                stale = ids[keep_per_provider:]
                if stale:
                    conn.execute(f"DELETE FROM {self.table} WHERE id IN ({','.join(str(int(i)) for i in stale)})")
                    removed += len(stale)
                prune = ids[DETAILED_JOBS_PER_PROVIDER:keep_per_provider]
                if prune:
                    conn.execute(
                        f"UPDATE {self.table} SET warnings = '[]', errors = '[]', details_pruned = 1 "
                        f"WHERE details_pruned = 0 AND id IN ({','.join(str(int(i)) for i in prune)})"
                    )
        if removed:
            log.info("History retention removed %d old %s rows", removed, self.table)
        return removed


class SyncHistoryRepository(_HistoryBase):
    table = "sync_jobs"
    summary_columns = ("created", "updated", "skipped", "missing")

    def _extra_list_columns(self) -> str:
        return ", sync_type"


class ProbeHistoryRepository(_HistoryBase):
    table = "probe_jobs"
    summary_columns = ("considered", "probed", "skipped", "succeeded", "failed")

    def _extra_list_columns(self) -> str:
        return ", scope, forced"
