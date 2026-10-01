"""Application logging: rotating file + console, with credential redaction on every record."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from collections import deque
from typing import Optional

from .redact import redact

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
LOG_FILE_NAME = "xtreamscraper.log"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5


class RedactingFormatter(logging.Formatter):
    """Formats the record normally, then strips credentials from the final text.

    Redacting the fully formatted output (message, args and traceback) is the only way
    to be sure a URL embedded in an exception message never reaches a log file.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class MemoryLogHandler(logging.Handler):
    """Keeps the last N redacted log lines for the WebUI log view."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)
        self.setFormatter(RedactingFormatter(LOG_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)


memory_handler = MemoryLogHandler()


def setup_logging(log_dir: str, level: str = "INFO", console: bool = True) -> str:
    """Configure the root logger. Returns the log file path."""
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, LOG_FILE_NAME)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = RedactingFormatter(LOG_FORMAT)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8", delay=True
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(memory_handler)
    if console:
        try:
            # A Windows console code page cannot show every title; never fail on that.
            sys.stderr.reconfigure(errors="backslashreplace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
    set_level(level)
    # Third-party request logging would print full URLs at DEBUG; keep it quiet.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("waitress").setLevel(logging.INFO)
    return log_path


def set_level(level: Optional[str]) -> None:
    value = getattr(logging, str(level or "INFO").upper(), logging.INFO)
    logging.getLogger().setLevel(value)


def recent_lines(limit: int = 200) -> list[str]:
    lines = list(memory_handler.lines)
    return lines[-limit:]
