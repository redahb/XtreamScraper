"""Ordered schema migrations.

Rules for future versions: never edit a released migration; append a new
``(version, [statements])`` entry (or a callable taking the connection) instead. Each
migration runs in its own transaction and is recorded in ``schema_version``.
"""

from __future__ import annotations

from typing import Callable, Sequence, Union

Migration = tuple[int, Union[Sequence[str], Callable]]

_V1 = [
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE providers (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        name                   TEXT NOT NULL,
        base_url               TEXT NOT NULL,
        username               TEXT NOT NULL,
        password               TEXT NOT NULL,
        -- 'plain' today; lets a later version store encrypted passwords side by side.
        password_scheme        TEXT NOT NULL DEFAULT 'plain',
        user_agent             TEXT NOT NULL DEFAULT '',
        target_folder          TEXT NOT NULL DEFAULT '',
        enabled                INTEGER NOT NULL DEFAULT 1,
        movies_enabled         INTEGER NOT NULL DEFAULT 1,
        series_enabled         INTEGER NOT NULL DEFAULT 1,
        created_at             TEXT NOT NULL,
        updated_at             TEXT NOT NULL,
        last_test_at           TEXT,
        last_test_ok           INTEGER,
        last_test_message      TEXT,
        categories_refreshed_at TEXT
    )
    """,
    """
    CREATE TABLE categories (
        provider_id   INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
        content_type  TEXT NOT NULL CHECK (content_type IN ('movie', 'series')),
        category_id   TEXT NOT NULL,
        name          TEXT NOT NULL,
        selected      INTEGER NOT NULL DEFAULT 0,
        present       INTEGER NOT NULL DEFAULT 1,
        folder_name   TEXT,
        first_seen    TEXT NOT NULL,
        last_seen     TEXT NOT NULL,
        PRIMARY KEY (provider_id, content_type, category_id)
    )
    """,
    """
    CREATE TABLE movies (
        provider_id        INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
        category_id        TEXT NOT NULL,
        stream_id          TEXT NOT NULL,
        title              TEXT NOT NULL,
        year               INTEGER,
        extension          TEXT NOT NULL,
        base_name          TEXT NOT NULL,
        folder_name        TEXT NOT NULL,
        folder_path        TEXT NOT NULL,
        strm_path          TEXT NOT NULL,
        url_hash           TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL,
        status             TEXT NOT NULL DEFAULT 'active',
        first_seen         TEXT NOT NULL,
        last_seen          TEXT NOT NULL,
        last_updated       TEXT NOT NULL,
        last_synced        TEXT NOT NULL,
        PRIMARY KEY (provider_id, category_id, stream_id)
    )
    """,
    """
    CREATE TABLE series (
        provider_id        INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
        category_id        TEXT NOT NULL,
        series_id          TEXT NOT NULL,
        title              TEXT NOT NULL,
        year               INTEGER,
        base_name          TEXT NOT NULL,
        folder_name        TEXT NOT NULL,
        folder_path        TEXT NOT NULL,
        last_modified      TEXT,
        info_fetched_at    TEXT,
        episode_count      INTEGER NOT NULL DEFAULT 0,
        source_fingerprint TEXT NOT NULL,
        status             TEXT NOT NULL DEFAULT 'active',
        first_seen         TEXT NOT NULL,
        last_seen          TEXT NOT NULL,
        last_updated       TEXT NOT NULL,
        last_synced        TEXT NOT NULL,
        PRIMARY KEY (provider_id, category_id, series_id)
    )
    """,
    """
    CREATE TABLE episodes (
        provider_id        INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
        category_id        TEXT NOT NULL,
        series_id          TEXT NOT NULL,
        episode_id         TEXT NOT NULL,
        season             INTEGER NOT NULL,
        episode            INTEGER NOT NULL,
        title              TEXT NOT NULL DEFAULT '',
        extension          TEXT NOT NULL,
        base_name          TEXT NOT NULL,
        file_name          TEXT NOT NULL,
        strm_path          TEXT NOT NULL,
        url_hash           TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL,
        status             TEXT NOT NULL DEFAULT 'active',
        first_seen         TEXT NOT NULL,
        last_seen          TEXT NOT NULL,
        last_updated       TEXT NOT NULL,
        last_synced        TEXT NOT NULL,
        PRIMARY KEY (provider_id, category_id, series_id, episode_id)
    )
    """,
    "CREATE INDEX idx_episodes_series ON episodes(provider_id, category_id, series_id)",
    """
    CREATE TABLE probe_state (
        provider_id      INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
        content_type     TEXT NOT NULL CHECK (content_type IN ('movie', 'episode')),
        category_id      TEXT NOT NULL,
        item_id          TEXT NOT NULL,
        series_id        TEXT,
        strm_path        TEXT NOT NULL,
        url_hash         TEXT,
        last_attempt_at  TEXT,
        last_success_at  TEXT,
        status           TEXT NOT NULL DEFAULT 'never',
        failed_attempts  INTEGER NOT NULL DEFAULT 0,
        last_error       TEXT,
        media_info       TEXT,
        media_schema     INTEGER,
        PRIMARY KEY (provider_id, content_type, category_id, item_id)
    )
    """,
    """
    CREATE TABLE sync_jobs (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id           TEXT NOT NULL,
        provider_id      INTEGER,
        provider_name    TEXT NOT NULL,
        sync_type        TEXT NOT NULL,
        status           TEXT NOT NULL,
        started_at       TEXT NOT NULL,
        finished_at      TEXT,
        duration_seconds REAL,
        created          INTEGER NOT NULL DEFAULT 0,
        updated          INTEGER NOT NULL DEFAULT 0,
        skipped          INTEGER NOT NULL DEFAULT 0,
        missing          INTEGER NOT NULL DEFAULT 0,
        warning_count    INTEGER NOT NULL DEFAULT 0,
        error_count      INTEGER NOT NULL DEFAULT 0,
        stats            TEXT NOT NULL DEFAULT '{}',
        warnings         TEXT NOT NULL DEFAULT '[]',
        errors           TEXT NOT NULL DEFAULT '[]',
        details_pruned   INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_sync_jobs_provider ON sync_jobs(provider_id, started_at)",
    """
    CREATE TABLE probe_jobs (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id           TEXT NOT NULL,
        provider_id      INTEGER,
        provider_name    TEXT NOT NULL,
        scope            TEXT NOT NULL,
        forced           INTEGER NOT NULL DEFAULT 0,
        status           TEXT NOT NULL,
        started_at       TEXT NOT NULL,
        finished_at      TEXT,
        duration_seconds REAL,
        considered       INTEGER NOT NULL DEFAULT 0,
        probed           INTEGER NOT NULL DEFAULT 0,
        skipped          INTEGER NOT NULL DEFAULT 0,
        succeeded        INTEGER NOT NULL DEFAULT 0,
        failed           INTEGER NOT NULL DEFAULT 0,
        warning_count    INTEGER NOT NULL DEFAULT 0,
        error_count      INTEGER NOT NULL DEFAULT 0,
        stats            TEXT NOT NULL DEFAULT '{}',
        warnings         TEXT NOT NULL DEFAULT '[]',
        errors           TEXT NOT NULL DEFAULT '[]',
        details_pruned   INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_probe_jobs_provider ON probe_jobs(provider_id, started_at)",
]

MIGRATIONS: list[Migration] = [
    (1, _V1),
]
