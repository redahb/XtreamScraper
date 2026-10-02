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

# v2: metadata scraper framework
_V2 = [
    """
    CREATE TABLE scraper_plugins (
        plugin_id   TEXT PRIMARY KEY,
        enabled     INTEGER NOT NULL DEFAULT 0,
        overwrite   INTEGER NOT NULL DEFAULT 0,
        priority    INTEGER NOT NULL,
        installed_at TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE scraper_config (
        plugin_id  TEXT PRIMARY KEY,
        config     TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE scraper_bindings (
        plugin_id              TEXT NOT NULL,
        item_kind              TEXT NOT NULL CHECK (item_kind IN ('movie', 'series', 'season', 'episode')),
        provider_id            INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
        category_id            TEXT NOT NULL,
        item_id                TEXT NOT NULL,
        remote_id              TEXT,
        remote_parent_id       TEXT,
        season_number          INTEGER,
        episode_number         INTEGER,
        match_method           TEXT,
        match_score            REAL,
        matched_title          TEXT,
        matched_year           INTEGER,
        matched_at             TEXT,
        last_successful_scrape TEXT,
        last_attempt           TEXT,
        status                 TEXT NOT NULL,
        message                TEXT,
        candidates             TEXT NOT NULL DEFAULT '[]',
        config_fingerprint     TEXT,
        PRIMARY KEY (plugin_id, item_kind, provider_id, category_id, item_id)
    )
    """,
    "CREATE INDEX idx_scraper_bindings_provider ON scraper_bindings(provider_id, item_kind)",
    """
    CREATE TABLE metadata_jobs (
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
        matched          INTEGER NOT NULL DEFAULT 0,
        unmatched        INTEGER NOT NULL DEFAULT 0,
        ambiguous        INTEGER NOT NULL DEFAULT 0,
        updated          INTEGER NOT NULL DEFAULT 0,
        unchanged        INTEGER NOT NULL DEFAULT 0,
        skipped          INTEGER NOT NULL DEFAULT 0,
        warning_count    INTEGER NOT NULL DEFAULT 0,
        error_count      INTEGER NOT NULL DEFAULT 0,
        plugins          TEXT NOT NULL DEFAULT '[]',
        stats            TEXT NOT NULL DEFAULT '{}',
        warnings         TEXT NOT NULL DEFAULT '[]',
        errors           TEXT NOT NULL DEFAULT '[]',
        details_pruned   INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_metadata_jobs_provider ON metadata_jobs(provider_id, started_at)",
]

# v3: core artwork management – persisted selection per artwork slot, ownership of local
# artwork files and of remote NFO references, and artwork job history.
_V3 = [
    """
    CREATE TABLE artwork_items (
        provider_id  INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
        item_kind    TEXT NOT NULL CHECK (item_kind IN ('movie', 'series', 'season', 'episode')),
        category_id  TEXT NOT NULL,
        item_id      TEXT NOT NULL,
        evaluated_at TEXT NOT NULL,
        PRIMARY KEY (provider_id, item_kind, category_id, item_id)
    )
    """,
    """
    CREATE TABLE artwork_slots (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id      INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
        item_kind        TEXT NOT NULL CHECK (item_kind IN ('movie', 'series', 'season', 'episode')),
        category_id      TEXT NOT NULL,
        item_id          TEXT NOT NULL,
        artwork_type     TEXT NOT NULL,
        source_plugin    TEXT,
        source_remote_id TEXT,
        source_url       TEXT NOT NULL,
        language         TEXT,
        width            INTEGER,
        height           INTEGER,
        selected_at      TEXT NOT NULL,
        mode             TEXT,
        status           TEXT NOT NULL DEFAULT 'pending',
        message          TEXT,
        updated_at       TEXT NOT NULL,
        UNIQUE (provider_id, item_kind, category_id, item_id, artwork_type)
    )
    """,
    "CREATE INDEX idx_artwork_slots_provider ON artwork_slots(provider_id, item_kind)",
    """
    CREATE TABLE artwork_files (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        slot_id       INTEGER NOT NULL REFERENCES artwork_slots(id) ON DELETE CASCADE,
        role          TEXT NOT NULL CHECK (role IN ('primary', 'alias')),
        local_path    TEXT NOT NULL,
        file_hash     TEXT NOT NULL,
        size          INTEGER NOT NULL,
        source_url    TEXT NOT NULL,
        source_plugin TEXT,
        status        TEXT NOT NULL DEFAULT 'managed',
        downloaded_at TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        UNIQUE (slot_id, local_path)
    )
    """,
    """
    CREATE TABLE artwork_nfo_refs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        slot_id       INTEGER NOT NULL REFERENCES artwork_slots(id) ON DELETE CASCADE,
        nfo_path      TEXT NOT NULL,
        nfo_kind      TEXT NOT NULL,
        location      TEXT NOT NULL,
        url           TEXT NOT NULL,
        source_plugin TEXT,
        written_at    TEXT NOT NULL,
        UNIQUE (slot_id, nfo_path, location, url)
    )
    """,
    "CREATE INDEX idx_artwork_refs_nfo ON artwork_nfo_refs(nfo_path, location)",
    """
    CREATE TABLE artwork_jobs (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id            TEXT NOT NULL,
        provider_id       INTEGER,
        provider_name     TEXT NOT NULL,
        mode              TEXT NOT NULL,
        forced            INTEGER NOT NULL DEFAULT 0,
        status            TEXT NOT NULL,
        started_at        TEXT NOT NULL,
        finished_at       TEXT,
        duration_seconds  REAL,
        considered        INTEGER NOT NULL DEFAULT 0,
        downloaded        INTEGER NOT NULL DEFAULT 0,
        nfo_urls_written  INTEGER NOT NULL DEFAULT 0,
        local_removed     INTEGER NOT NULL DEFAULT 0,
        nfo_refs_removed  INTEGER NOT NULL DEFAULT 0,
        unchanged         INTEGER NOT NULL DEFAULT 0,
        skipped           INTEGER NOT NULL DEFAULT 0,
        warning_count     INTEGER NOT NULL DEFAULT 0,
        error_count       INTEGER NOT NULL DEFAULT 0,
        stats             TEXT NOT NULL DEFAULT '{}',
        warnings          TEXT NOT NULL DEFAULT '[]',
        errors            TEXT NOT NULL DEFAULT '[]',
        details_pruned    INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_artwork_jobs_provider ON artwork_jobs(provider_id, started_at)",
]

# v4: plugin status reported by scrape jobs and connection tests (e.g. "request quota
# exhausted"), shown on the plugin's settings page. Never contains secrets.
_V4 = [
    """
    CREATE TABLE scraper_status (
        plugin_id   TEXT PRIMARY KEY,
        status      TEXT NOT NULL DEFAULT '{}',
        updated_at  TEXT NOT NULL
    )
    """,
]

# v5: missing-item lifecycle (reversible .strm quarantine, optional purge), confirmation of
# suspicious empty category answers, and ownership of NFO files the application created.
# Rows that were already 'missing' keep missing_since NULL: their lifecycle starts at the
# next successful sync that confirms they are still gone, so they are never purged at once.
_V5 = [
    "ALTER TABLE movies ADD COLUMN missing_since TEXT",
    "ALTER TABLE movies ADD COLUMN quarantined_at TEXT",
    "ALTER TABLE movies ADD COLUMN quarantine_path TEXT",
    "ALTER TABLE series ADD COLUMN missing_since TEXT",
    "ALTER TABLE episodes ADD COLUMN missing_since TEXT",
    "ALTER TABLE episodes ADD COLUMN quarantined_at TEXT",
    "ALTER TABLE episodes ADD COLUMN quarantine_path TEXT",
    "ALTER TABLE categories ADD COLUMN empty_syncs INTEGER NOT NULL DEFAULT 0",
    """
    CREATE TABLE nfo_files (
        nfo_path     TEXT PRIMARY KEY,
        nfo_kind     TEXT NOT NULL,
        provider_id  INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
        item_kind    TEXT NOT NULL CHECK (item_kind IN ('movie', 'series', 'season', 'episode')),
        category_id  TEXT NOT NULL,
        item_id      TEXT NOT NULL,
        file_hash    TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'managed',
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_nfo_files_item ON nfo_files(provider_id, item_kind, category_id, item_id)",
]

# v6: IDs a user entered by hand and confirmed (``{namespace: value}``), on the binding that
# was matched with them. They are the item's authoritative IDs: no plugin may change them.
_V6 = [
    "ALTER TABLE scraper_bindings ADD COLUMN manual_ids TEXT NOT NULL DEFAULT '{}'",
]

MIGRATIONS: list[Migration] = [
    (1, _V1),
    (2, _V2),
    (3, _V3),
    (4, _V4),
    (5, _V5),
    (6, _V6),
]
