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
    """Configure the root logger. Returns the log file path.

    Contract:
        Preconditions:
            - Replaces (and closes) every handler already on the root logger, so it is meant
              to be called once at start-up (or again in tests).

        Raises:
            OSError: When ``log_dir`` cannot be created (from ``os.makedirs``). The log file
                itself is opened lazily (``delay=True``), so a file that cannot be written fails
                on the first record, through ``logging``'s own error handling.

        Silences:
            - ``ValueError`` from ``sys.stderr.reconfigure`` and a stderr without it (None
              under pythonw): the console keeps its own error handling.
            - An invalid ``level`` name gives INFO (see :func:`set_level`).
    """
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
        # A Windows console code page cannot show every title; never fail on that. stderr may
        # be None (pythonw) or a replacement stream without reconfigure().
        reconfigure = getattr(sys.stderr, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except ValueError:
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
    """Set the root logger level from a level name (any case); ``None``, empty or unknown names
    (and ``logging`` attributes that are not levels) give INFO without an error."""
    value = getattr(logging, str(level or "INFO").upper(), logging.INFO)
    if isinstance(value, bool) or not isinstance(value, int):  # e.g. logging.BASIC_FORMAT
        value = logging.INFO
    logging.getLogger().setLevel(value)


def recent_lines(limit: int = 200) -> list[str]:
    """The newest ``limit`` redacted lines kept for the WebUI (oldest first); none for ``limit <= 0``.

    At most the handler's capacity (500) lines are kept.
    """
    if limit <= 0:
        return []  # lines[-0:] would be every line
    lines = list(memory_handler.lines)
    return lines[-limit:]
