"""SQLite access: one connection per thread, WAL mode, explicit transactions, migrations."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from .migrations import MIGRATIONS

log = logging.getLogger(__name__)


def _undo(conn: sqlite3.Connection, *statements: str) -> None:
    """Undo a failed transaction (or savepoint) while an exception is propagating.

    Some errors (disk full, I/O errors, an ``OR ROLLBACK`` conflict) make SQLite end the
    whole transaction by itself; then there is nothing left to undo and running ``ROLLBACK``
    would raise "no transaction is active" in place of the real cause. A failure of the undo
    itself is logged, so the caller still sees the original exception.
    """
    if not conn.in_transaction:
        return
    try:
        for statement in statements:
            conn.execute(statement)
    except sqlite3.Error as exc:
        log.warning("Database rollback failed: %s", exc)


#: Positional (``?``) or named (``:name``) SQL parameters.
Params = Sequence[Any] | Mapping[str, Any]


class Database:
    """The application's SQLite database: one connection per thread, explicit write transactions.

    Contract:
        Preconditions:
            - ``path`` is a file path. ``":memory:"`` is accepted but each thread then gets its
              own, separate, empty database (connections are per thread), so only the thread
              that ran :meth:`migrate` sees the schema. Nothing in the app uses it.
        Raises:
            OSError: From ``os.makedirs`` when the parent folder cannot be created.
    """

    path: str
    #: Per thread: ``conn`` (the thread's connection or None) and ``depth`` (transaction nesting).
    _local: threading.local
    _all_connections: list[sqlite3.Connection]
    _conn_lock: threading.Lock

    def __init__(self, path: str) -> None:
        self.path = path
        self._local = threading.local()
        self._all_connections: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    # -- connections -------------------------------------------------------------------
    def connection(self) -> sqlite3.Connection:
        """The calling thread's connection, opened on first use (autocommit, WAL, foreign keys on).

        Contract:
            Raises:
                sqlite3.Error: When the file cannot be opened or the PRAGMAs fail (not caught;
                    nothing is cached then, so the next call retries).
        """
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

    def release_thread(self) -> None:
        """Close the calling thread's connection and forget it (idempotent).

        Worker and job threads call this when they finish so short-lived threads do not
        leave SQLite handles open until shutdown. The thread gets a fresh connection if it
        uses the database again. Not allowed inside a transaction.
        """
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is None:
            return
        if getattr(self._local, "depth", 0) > 0:
            raise RuntimeError("release_thread() called inside a transaction")
        self._local.conn = None
        self._local.depth = 0
        with self._conn_lock:
            self._all_connections = [c for c in self._all_connections if c is not conn]
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass

    def open_connections(self) -> int:
        """Number of connections currently open across all threads."""
        with self._conn_lock:
            return len(self._all_connections)

    def close(self) -> None:
        """Close every thread's connection; later calls open fresh ones.

        Contract:
            Preconditions:
                - No thread is using its connection. A transaction in progress on another
                  thread is cut off (SQLite rolls it back) and that thread's next statement
                  fails with ``sqlite3.ProgrammingError`` (closed connection).
            Silences:
                - ``sqlite3.Error`` from ``Connection.close``.
        """
        with self._conn_lock:
            for conn in self._all_connections:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover
                    pass
            self._all_connections.clear()
        self._local = threading.local()

    # -- helpers -------------------------------------------------------------------------
    def query(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        return self.connection().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Params = ()) -> Optional[sqlite3.Row]:
        return self.connection().execute(sql, params).fetchone()

    def execute(self, sql: str, params: Params = ()) -> sqlite3.Cursor:
        return self.connection().execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        """Run ``sql`` once per row, all in one write transaction (all rows or none)."""
        with self.transaction() as conn:
            conn.executemany(sql, rows)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Write transaction. Nested use on the same thread joins the outer transaction
        through a savepoint: a failing inner block undoes only its own writes (and the
        exception still propagates).

        Contract:
            Preconditions:
                - The block uses only the yielded connection (or this thread's
                  :meth:`connection`); another thread's writes wait for the ``BEGIN IMMEDIATE``
                  lock (``busy_timeout`` 30 s, then ``sqlite3.OperationalError``
                  "database is locked").
                - A caller that catches an inner block's exception and carries on keeps
                  writing in the outer transaction; if SQLite itself ended the transaction
                  (disk full, ``OR ROLLBACK``), later statements run in autocommit mode and the
                  final ``COMMIT`` raises ``sqlite3.OperationalError``.
            Raises:
                BaseException: Whatever the block raises, unchanged, after the undo.
                sqlite3.Error: From ``BEGIN IMMEDIATE`` / ``SAVEPOINT`` / ``RELEASE`` /
                    ``COMMIT`` (a failed ``COMMIT`` is rolled back first).
            Silences:
                - A failure of the undo itself (``ROLLBACK`` / ``ROLLBACK TO``) is logged as a
                  warning so the original exception still propagates.
        """
        conn = self.connection()
        depth = getattr(self._local, "depth", 0)
        if depth > 0:
            savepoint = f"sp_{depth}"
            conn.execute(f"SAVEPOINT {savepoint}")
            self._local.depth = depth + 1
            try:
                yield conn
            except BaseException:
                _undo(conn, f"ROLLBACK TO {savepoint}", f"RELEASE {savepoint}")
                raise
            else:
                conn.execute(f"RELEASE {savepoint}")
            finally:
                self._local.depth = depth
            return
        conn.execute("BEGIN IMMEDIATE")
        self._local.depth = 1
        try:
            yield conn
        except BaseException:
            _undo(conn, "ROLLBACK")
            raise
        else:
            try:
                conn.execute("COMMIT")
            except BaseException:
                # e.g. a deferred constraint fails at COMMIT: the transaction is still open.
                _undo(conn, "ROLLBACK")
                raise
        finally:
            self._local.depth = 0

    # -- schema --------------------------------------------------------------------------
    def schema_version(self) -> int:
        """Highest applied migration (0 for a new database); creates ``schema_version`` if needed."""
        conn = self.connection()
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"] or 0)

    def migrate(self) -> int:
        """Apply pending migrations in order. Existing data is always kept.

        Contract:
            Raises:
                sqlite3.Error: From a failing statement; that migration is rolled back as a
                    whole (one transaction per version), earlier ones stay applied.
            Silences:
                - A database already at a version newer than :data:`MIGRATIONS` knows (written
                  by a later app version) is used as is, without a warning; the return value
                  is then that newer version.
        """
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
