"""Application-wide settings, persisted as key/value rows in the SQLite database."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from typing import Any, Mapping

from ..storage.db import Database

log = logging.getLogger(__name__)

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass
class Settings:
    # Web server (changes need a restart)
    web_host: str = "127.0.0.1"
    web_port: int = 6060
    # Library
    default_target_folder: str = ""
    clean_titles: bool = True
    episode_title_in_filename: bool = False
    max_name_length: int = 120
    # Logging
    log_level: str = "INFO"
    # Xtream HTTP
    http_connect_timeout: float = 15.0
    http_read_timeout: float = 120.0
    http_retries: int = 3
    request_delay_ms: int = 0
    # Series incremental sync: re-fetch series info at least this often even when the
    # provider's last_modified value says nothing changed (0 = always re-fetch).
    series_full_refresh_days: int = 7
    # Probing
    ffprobe_path: str = ""
    probe_timeout_seconds: int = 60
    probe_analyze_duration_ms: int = 5000
    probe_size_kb: int = 5000
    probe_concurrency: int = 1
    probe_retry_failed_after_hours: int = 24
    probe_max_failed_attempts: int = 5
    probe_stale_days: int = 0  # 0 = never consider successful probes stale
    # Metadata scraping: retry unmatched/ambiguous/missing items after this many days
    metadata_retry_unmatched_days: int = 7
    # Artwork (core artwork manager; applies to every scraper plugin)
    artwork_mode: str = "local"  # local | remote | disabled
    artwork_movie_poster: bool = True
    artwork_movie_fanart: bool = True
    artwork_show_poster: bool = True
    artwork_show_fanart: bool = True
    artwork_season_poster: bool = True
    artwork_episode_still: bool = True
    artwork_clearlogo: bool = False
    artwork_banner: bool = False
    artwork_landscape: bool = False
    artwork_keyart: bool = False
    artwork_existing: str = "keep"  # keep | replace_managed
    artwork_aliases: bool = True  # SeasonXX.jpg / <episode>.jpg compatibility copies
    artwork_keep_previous: bool = False  # keep the previous representation when the mode changes
    artwork_timeout_seconds: int = 30
    artwork_retries: int = 3
    artwork_max_size_mb: int = 20
    # History retention
    history_keep_per_provider: int = 100
    history_max_age_days: int = 180

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_BOUNDS: dict[str, tuple[float, float]] = {
    "web_port": (1, 65535),
    "max_name_length": (40, 200),
    "http_connect_timeout": (1, 300),
    "http_read_timeout": (5, 3600),
    "http_retries": (0, 10),
    "request_delay_ms": (0, 60000),
    "series_full_refresh_days": (0, 3650),
    "probe_timeout_seconds": (5, 3600),
    "probe_analyze_duration_ms": (500, 120000),
    "probe_size_kb": (32, 200000),
    "probe_concurrency": (1, 16),
    "probe_retry_failed_after_hours": (0, 8760),
    "probe_max_failed_attempts": (1, 1000),
    "probe_stale_days": (0, 3650),
    "metadata_retry_unmatched_days": (0, 3650),
    "artwork_timeout_seconds": (5, 600),
    "artwork_retries": (0, 10),
    "artwork_max_size_mb": (1, 200),
    "history_keep_per_provider": (5, 10000),
    "history_max_age_days": (1, 36500),
}


_CHOICES: dict[str, tuple[str, ...]] = {
    "artwork_mode": ("local", "remote", "disabled"),
    "artwork_existing": ("keep", "replace_managed"),
}

#: Settings whose change requires artwork reconciliation of the existing library.
ARTWORK_RECONCILE_KEYS = (
    "artwork_mode", "artwork_movie_poster", "artwork_movie_fanart", "artwork_show_poster", "artwork_show_fanart",
    "artwork_season_poster", "artwork_episode_still", "artwork_clearlogo", "artwork_banner", "artwork_landscape",
    "artwork_keyart", "artwork_existing", "artwork_aliases",
)


class SettingsError(ValueError):
    pass


def coerce_settings(values: Mapping[str, Any], base: Settings | None = None) -> Settings:
    """Validate and convert raw values (e.g. from JSON) onto ``base``. Unknown keys are ignored."""
    current = asdict(base or Settings())
    for f in fields(Settings):
        if f.name not in values:
            continue
        raw = values[f.name]
        default = getattr(Settings, f.name)
        try:
            if isinstance(default, bool):
                if isinstance(raw, str):
                    value: Any = raw.strip().lower() in ("1", "true", "yes", "on")
                else:
                    value = bool(raw)
            elif isinstance(default, int):
                value = int(float(raw))
            elif isinstance(default, float):
                value = float(raw)
            else:
                value = "" if raw is None else str(raw).strip()
        except (TypeError, ValueError):
            raise SettingsError(f"Invalid value for {f.name}") from None
        if f.name in _BOUNDS:
            low, high = _BOUNDS[f.name]
            if not low <= value <= high:
                raise SettingsError(f"{f.name} must be between {low:g} and {high:g}")
        if f.name in _CHOICES:
            value = str(value).lower()
            if value not in _CHOICES[f.name]:
                raise SettingsError(f"{f.name} must be one of {', '.join(_CHOICES[f.name])}")
        if f.name == "log_level":
            value = str(value).upper()
            if value not in LOG_LEVELS:
                raise SettingsError(f"log_level must be one of {', '.join(LOG_LEVELS)}")
        current[f.name] = value
    return Settings(**current)


class SettingsStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def load(self) -> Settings:
        stored: dict[str, Any] = {}
        for row in self.db.query("SELECT key, value FROM settings"):
            try:
                stored[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                log.warning("Ignoring unreadable setting %s", row["key"])
        try:
            return coerce_settings(stored)
        except SettingsError as exc:
            # A single bad stored value must not stop the app from starting.
            log.warning("Stored settings invalid (%s); falling back per field", exc)
            result = Settings()
            for key, value in stored.items():
                try:
                    result = coerce_settings({key: value}, result)
                except SettingsError:
                    log.warning("Resetting invalid setting %s to default", key)
            return result

    def save(self, values: Mapping[str, Any]) -> Settings:
        merged = coerce_settings(values, self.load())
        with self.db.transaction() as conn:
            for key, value in merged.to_dict().items():
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value)),
                )
        return merged
