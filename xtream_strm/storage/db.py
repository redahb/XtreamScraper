"""SQLite access: one connection per thread, WAL mode, explicit transactions, migrations."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional, Sequence

from .migrations import MIGRATIONS

log = logging.getLogger(__name__)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._local = threading.local()
        self._all_connections: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    # -- connections -------------------------------------------------------------------
    def connection(self) -> sqlite3.Connection:
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
            self._local.conn = conn
            self._local.depth = 0
            with self._conn_lock:
                self._all_connections.append(conn)
        return conn

    def close(self) -> None:
        with self._conn_lock:
            for conn in self._all_connections:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover
                    pass
            self._all_connections.clear()
        self._local = threading.local()

    # -- helpers -------------------------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] | dict = ()) -> list[sqlite3.Row]:
        return self.connection().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] | dict = ()) -> Optional[sqlite3.Row]:
        return self.connection().execute(sql, params).fetchone()

    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Cursor:
        return self.connection().execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self.transaction() as conn:
            conn.executemany(sql, rows)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Write transaction. Nested use on the same thread joins the outer transaction."""
        conn = self.connection()
        depth = getattr(self._local, "depth", 0)
        if depth > 0:
            self._local.depth = depth + 1
            try:
                yield conn
            finally:
                self._local.depth = depth
            return
        conn.execute("BEGIN IMMEDIATE")
        self._local.depth = 1
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        finally:
            self._local.depth = 0

    # -- schema --------------------------------------------------------------------------
    def schema_version(self) -> int:
        conn = self.connection()
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"] or 0)

    def migrate(self) -> int:
        """Apply pending migrations in order. Existing data is always kept."""
        current = self.schema_version()
        for version, statements in MIGRATIONS:
            if version <= current:
                continue
            log.info("Applying database migration %d", version)
            with self.transaction() as conn:
                if callable(statements):
                    statements(conn)
                else:
                    for statement in statements:
                        conn.execute(statement)
                conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
            current = version
        return current
