"""Media probing of an existing library.

Independent from synchronization: items come from the library index (sync state), the
stream URL is read from each ``.strm`` file on disk, ffprobe runs against that URL and
the result is merged into the item's NFO ``<fileinfo>`` through the shared NFO service.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional, Sequence

from ..config.settings import Settings
from ..filesystem import atomic
from ..filesystem.naming import trustworthy_episode_title
from ..filesystem.strm import is_usable_url, read_strm_url
from ..jobs.progress import JobCancelled, JobProgress
from ..library.index import LibraryIndex
from ..library.models import EPISODE, MOVIE, LibraryItem
from ..nfo.document import NfoError
from ..nfo.fileinfo import apply_media_info
from ..nfo.service import NfoSeed, update_nfo
from ..storage.db import Database
from ..storage.probe_state import (
    STATUS_FAILED,
    STATUS_INVALID_STRM,
    STATUS_OK,
    ProbeRecord,
    ProbeStateRepository,
)
from ..utils.hashing import url_fingerprint
from ..utils.redact import redact
from ..utils.timeutil import now_iso
from .ffprobe import ProbeError, ProbeOptions, parse_ffprobe_output, run_ffprobe
from .models import MEDIA_SCHEMA_VERSION, MediaInfo
from .policy import ProbePolicy, needs_probe

log = logging.getLogger(__name__)

Runner = Callable[[str, str, ProbeOptions], dict]
MAX_MESSAGES = 500


@dataclass
class ProbeStats:
    considered: int = 0
    probed: int = 0
    skipped_unchanged: int = 0
    skipped_policy: int = 0
    succeeded: int = 0
    failed: int = 0
    invalid_strm: int = 0
    missing_files: int = 0
    nfo_created: int = 0
    nfo_updated: int = 0
    nfo_errors: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        if len(self.warnings) < MAX_MESSAGES:
            self.warnings.append(redact(message))

    def error(self, message: str) -> None:
        if len(self.errors) < MAX_MESSAGES:
            self.errors.append(redact(message))

    def counters(self) -> dict[str, int]:
        return {k: v for k, v in asdict(self).items() if isinstance(v, int)}

    def summary(self) -> dict[str, int]:
        return {
            "considered": self.considered,
            "probed": self.probed,
            "skipped": self.skipped_unchanged + self.skipped_policy,
            "succeeded": self.succeeded,
            "failed": self.failed + self.invalid_strm,
        }


@dataclass
class ProbeRequest:
    provider_id: Optional[int] = None
    kinds: Sequence[str] = (MOVIE, EPISODE)
    force: bool = False

    @property
    def scope(self) -> str:
        if set(self.kinds) == {MOVIE, EPISODE}:
            return "all"
        return "movies" if MOVIE in self.kinds else "series"


class ProbeEngine:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        ffprobe_path: str,
        progress: JobProgress,
        user_agent: str = "",
        runner: Runner = run_ffprobe,
    ) -> None:
        self.db = db
        self.settings = settings
        self.ffprobe_path = ffprobe_path
        self.progress = progress
        self.runner = runner
        self.state = ProbeStateRepository(db)
        self.index = LibraryIndex(db)
        self.options = ProbeOptions(
            timeout_seconds=settings.probe_timeout_seconds,
            analyze_duration_ms=settings.probe_analyze_duration_ms,
            probe_size_kb=settings.probe_size_kb,
            user_agent=user_agent,
        )
        self.policy = ProbePolicy(
            retry_failed_after_hours=settings.probe_retry_failed_after_hours,
            max_failed_attempts=settings.probe_max_failed_attempts,
            stale_days=settings.probe_stale_days,
        )
        self.stats = ProbeStats()
        self._stats_lock = threading.Lock()

    # -- entry point ---------------------------------------------------------------------------
    def run(self, request: ProbeRequest, provider_label: str = "") -> tuple[str, ProbeStats]:
        log.info("Probe started: %s (%s%s)", provider_label or "all providers", request.scope,
                 ", forced" if request.force else "")
        status = "success"
        try:
            self._run(request)
        except JobCancelled:
            status = "cancelled"
            self.stats.warn("Probing cancelled by user")
        except Exception as exc:
            status = "failed"
            self.stats.error(f"Unexpected error: {redact(exc)}")
            log.exception("Unexpected probe error")
        if status == "success" and (self.stats.errors or self.stats.failed or self.stats.invalid_strm):
            status = "partial"
        s = self.stats
        log.info(
            "Probe %s: %s – considered %d, probed %d, skipped %d, ok %d, failed %d",
            status, provider_label or "all providers", s.considered, s.probed,
            s.skipped_unchanged + s.skipped_policy, s.succeeded, s.failed + s.invalid_strm,
        )
        return status, self.stats

    def _run(self, request: ProbeRequest) -> None:
        items = list(self.index.items(provider_id=request.provider_id, kinds=request.kinds))
        existing: dict[tuple[int, str, str, str], ProbeRecord] = {}
        for provider_id in {i.provider_id for i in items}:
            for key, rec in self.state.all_for_provider(provider_id).items():
                existing[(provider_id, *key)] = rec
        self.progress.update(phase="Checking", total=len(items))
        self.stats.considered = len(items)

        todo: list[tuple[LibraryItem, str, Optional[ProbeRecord]]] = []
        for item in items:
            self.progress.check_cancelled()
            record = existing.get((item.provider_id, item.kind, item.category_id, item.item_id))
            prepared = self._prepare(item, record, request.force)
            if prepared is None:
                self.progress.advance()
            else:
                todo.append((item, prepared, record))

        self.progress.update(phase="Probing")
        workers = max(1, int(self.settings.probe_concurrency))
        if workers == 1:
            for item, url, record in todo:
                self.progress.check_cancelled()
                self._probe_item(item, url, record)
                self.progress.advance()
            return
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe") as pool:
            pending: set[Future] = set()
            queue = iter(todo)
            for item, url, record in queue:
                if self.progress.cancelled:
                    break
                pending.add(pool.submit(self._probe_item, item, url, record))
                if len(pending) >= workers * 2:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    self._collect(done)
            done, _ = wait(pending)
            self._collect(done)
        self.progress.check_cancelled()

    def _collect(self, futures) -> None:
        for future in futures:
            self.progress.advance()
            exc = future.exception()
            if exc is not None:  # _probe_item handles its own errors; this is a safety net
                self.stats.error(f"Probe worker error: {exc}")

    # -- per item --------------------------------------------------------------------------------
    def _prepare(self, item: LibraryItem, record: Optional[ProbeRecord], force: bool) -> Optional[str]:
        """Return the URL to probe, or None when the item is skipped."""
        if not atomic.is_file(item.strm_path):
            self.stats.missing_files += 1
            self.stats.warn(f"{item.label}: STRM file not found ({item.strm_path}); run a sync to recreate it")
            return None
        url = read_strm_url(item.strm_path)
        if not is_usable_url(url):
            self.stats.invalid_strm += 1
            self.stats.error(f"{item.label}: STRM does not contain a usable URL")
            self._save(item, record, None, STATUS_INVALID_STRM, "STRM does not contain a usable URL", None)
            return None
        assert url is not None
        should, reason = needs_probe(record, url_fingerprint(url), self.policy, force)
        if not should:
            if reason == "unchanged":
                self.stats.skipped_unchanged += 1
            else:
                self.stats.skipped_policy += 1
            return None
        return url

    def _probe_item(self, item: LibraryItem, url: str, record: Optional[ProbeRecord]) -> None:
        url_hash = url_fingerprint(url)
        self.progress.update(current_item=item.label, provider=self.progress.provider)
        with self._stats_lock:
            self.stats.probed += 1
        try:
            media = parse_ffprobe_output(self.runner(self.ffprobe_path, url, self.options))
            if not media.has_useful_data():
                raise ProbeError("ffprobe found no usable audio/video stream information")
        except ProbeError as exc:
            self._fail(item, record, url_hash, str(exc))
            return
        except Exception as exc:
            log.exception("Unexpected error probing %s", item.label)
            self._fail(item, record, url_hash, f"Unexpected error: {exc}")
            return

        try:
            result = update_nfo(
                item.nfo_path,
                item.nfo_kind,
                lambda doc: apply_media_info(doc, media),
                NfoSeed(
                    title=item.title if item.kind == MOVIE else (
                        trustworthy_episode_title(item.series_title or "", item.season or 0, item.episode, item.title)
                        or None
                    ),
                    year=item.year,
                    season=item.season,
                    episode=item.episode,
                    show_title=item.series_title,
                ),
            )
        except (NfoError, OSError) as exc:
            with self._stats_lock:
                self.stats.nfo_errors += 1
            log.error("NFO write failed for %s: %s", item.nfo_path, exc)
            # Keep the media info, but mark failed so the NFO write is retried later.
            self._fail(item, record, url_hash, f"NFO {item.nfo_path}: {exc}", media)
            return
        with self._stats_lock:
            self.stats.succeeded += 1
            if result.created:
                self.stats.nfo_created += 1
            elif result.written:
                self.stats.nfo_updated += 1
        now = now_iso()
        self._save(item, record, url_hash, STATUS_OK, None, media, attempt_at=now, success_at=now, failures=0)

    def _fail(
        self,
        item: LibraryItem,
        record: Optional[ProbeRecord],
        url_hash: str,
        message: str,
        media: Optional[MediaInfo] = None,
    ) -> None:
        message = redact(message)
        with self._stats_lock:
            self.stats.failed += 1
            self.stats.error(f"{item.label}: {message}")
        self.progress.error(f"{item.label}: {message}")
        log.warning("Probe failed for %s: %s", item.label, message)
        previous_failures = record.failed_attempts if record and record.url_hash == url_hash else 0
        self._save(item, record, url_hash, STATUS_FAILED, message, media,
                   attempt_at=now_iso(), failures=previous_failures + 1)

    def _save(
        self,
        item: LibraryItem,
        record: Optional[ProbeRecord],
        url_hash: Optional[str],
        status: str,
        error: Optional[str],
        media: Optional[MediaInfo],
        attempt_at: Optional[str] = None,
        success_at: Optional[str] = None,
        failures: Optional[int] = None,
    ) -> None:
        # A failed probe keeps previously detected media information.
        media_json = media.to_json() if media else (record.media_info if record else None)
        new = ProbeRecord(
            provider_id=item.provider_id,
            content_type=item.kind,
            category_id=item.category_id,
            item_id=item.item_id,
            series_id=item.series_id,
            strm_path=item.strm_path,
            url_hash=url_hash if url_hash is not None else (record.url_hash if record else None),
            last_attempt_at=attempt_at or now_iso(),
            last_success_at=success_at or (record.last_success_at if record else None),
            status=status,
            failed_attempts=failures if failures is not None else (record.failed_attempts if record else 0),
            last_error=error,
            media_info=media_json,
            media_schema=MEDIA_SCHEMA_VERSION if media_json else None,
        )
        try:
            self.state.save(new)
        except Exception:
            log.exception("Could not save probe state for %s", item.label)


def probe_summary_dict(stats: ProbeStats) -> dict[str, Any]:
    return {**stats.counters(), **stats.summary()}
