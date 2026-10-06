"""Thread-safe progress of a running job, polled by the WebUI."""

from __future__ import annotations

import threading
import uuid
from collections import deque
from typing import Any, Optional

from ..utils.redact import redact
from ..utils.timeutil import now_iso, seconds_between

RUNNING_STATES = ("queued", "running")


class JobCancelled(BaseException):
    """Raised by :meth:`JobProgress.check_cancelled`. A ``BaseException`` so the many
    ``except Exception`` blocks that isolate one failing item never swallow a cancel."""


class JobProgress:
    def __init__(self, kind: str, description: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.description = description
        self.status = "queued"
        self.created_at = now_iso()
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.provider: Optional[str] = None
        self.phase: Optional[str] = None
        self.category: Optional[str] = None
        self.current_item: Optional[str] = None
        self.processed = 0
        self.total = 0
        self.errors = 0
        self.warnings = 0
        self.recent_errors: deque[str] = deque(maxlen=25)
        self.summary: dict[str, Any] = {}
        self.history_ids: list[int] = []
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            self.status = "running"
            self.started_at = now_iso()

    def finish(self, status: str, summary: Optional[dict[str, Any]] = None) -> None:
        """Mark the job finished with ``status``.

        Contract:
            Preconditions:
                - ``status`` should not be ``queued``/``running``: the job would stay ``active``
                  (no check; nothing evicts it then).
            Silences:
                - A missing or empty ``summary`` keeps the summary set so far (the engines'
                  live figures) instead of clearing it.
        """
        with self._lock:
            self.status = status
            self.finished_at = now_iso()
            self.current_item = None
            if summary:
                self.summary = summary

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled()

    @property
    def active(self) -> bool:
        return self.status in RUNNING_STATES

    # -- updates -------------------------------------------------------------------------------
    def update(self, **values: Any) -> None:
        """Set attributes by name under the lock (``provider``, ``phase``, ``current_item`` ...).

        Contract:
            Silences:
                - Any key is accepted: a misspelt one creates a new attribute that ``to_dict``
                  never shows, and a key such as ``status`` or ``errors`` overwrites job state.
        """
        with self._lock:
            for key, value in values.items():
                setattr(self, key, value)

    def advance(self, count: int = 1) -> None:
        with self._lock:
            self.processed += count

    def add_total(self, count: int) -> None:
        with self._lock:
            self.total += count

    def error(self, message: str) -> None:
        """Count an error and keep its redacted text among the last 25 shown in the WebUI."""
        with self._lock:
            self.errors += 1
            self.recent_errors.append(redact(message))

    def warning(self) -> None:
        with self._lock:
            self.warnings += 1

    def to_dict(self) -> dict[str, Any]:
        """A consistent JSON-ready snapshot for the WebUI.

        Contract:
            Silences:
                - ``current_item`` is redacted here and ``recent_errors`` when recorded;
                  ``description``, ``provider``, ``phase``, ``category`` and ``summary`` are
                  passed as given, so callers must not put secrets in them.
        """
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "description": self.description,
                "status": self.status,
                "active": self.status in RUNNING_STATES,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_seconds": seconds_between(self.started_at, self.finished_at or now_iso()),
                "provider": self.provider,
                "phase": self.phase,
                "category": self.category,
                "current_item": redact(self.current_item) if self.current_item else None,
                "processed": self.processed,
                "total": self.total,
                "errors": self.errors,
                "warnings": self.warnings,
                "recent_errors": list(self.recent_errors),
                "summary": self.summary,
                "history_ids": list(self.history_ids),
                "cancel_requested": self._cancel.is_set(),
            }
