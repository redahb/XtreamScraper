"""Per-run synchronization statistics (what the history view shows)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..utils.redact import redact

MAX_MESSAGES = 500


@dataclass
class SyncStats:
    categories_processed: int = 0
    categories_failed: int = 0
    movies_discovered: int = 0
    movies_created: int = 0
    movies_updated: int = 0
    movies_unchanged: int = 0
    movies_failed: int = 0
    movies_missing: int = 0
    series_discovered: int = 0
    series_processed: int = 0
    series_unchanged: int = 0
    series_failed: int = 0
    series_missing: int = 0
    episodes_discovered: int = 0
    episodes_created: int = 0
    episodes_updated: int = 0
    episodes_unchanged: int = 0
    episodes_failed: int = 0
    episodes_missing: int = 0
    movies_quarantined: int = 0
    episodes_quarantined: int = 0
    movies_restored: int = 0
    episodes_restored: int = 0
    movies_purged: int = 0
    series_purged: int = 0
    episodes_purged: int = 0
    files_purged: int = 0
    purge_failures: int = 0
    categories_unconfirmed: int = 0  # empty or no longer listed: nothing marked missing
    files_recreated: int = 0
    items_relocated: int = 0
    name_collisions: int = 0
    categories: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        if len(self.warnings) < MAX_MESSAGES:
            self.warnings.append(redact(message))

    def error(self, message: str) -> None:
        if len(self.errors) < MAX_MESSAGES:
            self.errors.append(redact(message))

    @property
    def created(self) -> int:
        return self.movies_created + self.episodes_created

    @property
    def updated(self) -> int:
        return self.movies_updated + self.episodes_updated

    @property
    def unchanged(self) -> int:
        return self.movies_unchanged + self.episodes_unchanged

    @property
    def missing(self) -> int:
        return self.movies_missing + self.series_missing + self.episodes_missing

    def counters(self) -> dict[str, int]:
        return {k: v for k, v in asdict(self).items() if isinstance(v, int)}

    def summary(self) -> dict[str, int]:
        return {"created": self.created, "updated": self.updated, "skipped": self.unchanged, "missing": self.missing}
