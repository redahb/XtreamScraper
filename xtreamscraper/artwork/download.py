"""Artwork downloads: bounded retries, timeouts, size limits and payload validation.

Only a validated image is ever handed back; callers write it atomically. Error messages
go through the general redaction policy, so secrets in query strings never reach logs.
"""

from __future__ import annotations

import email.utils
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from .. import APP_NAME, __version__
from ..utils.redact import redact

log = logging.getLogger(__name__)

MAX_RETRY_AFTER = 60.0
_HTML_PREFIXES = (b"<!doctype", b"<html", b"<?xml", b"<head", b"<body", b"{", b"[")


class ArtworkDownloadError(Exception):
    """Download failed; ``permanent`` errors (404, invalid image...) are not worth retrying soon."""

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass
class DownloadedImage:
    data: bytes
    format: str  # jpeg | png | webp | gif

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def detect_format(data: bytes) -> Optional[str]:
    """Image format from magic bytes, or ``None`` when the payload is not a supported image."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def validate_image(data: bytes, content_type: str = "") -> str:
    """Return the image format or raise :class:`ArtworkDownloadError`."""
    if not data:
        raise ArtworkDownloadError("empty (zero-byte) response", permanent=True)
    lowered = data[:512].lstrip().lower()
    if content_type.split(";")[0].strip().lower() in ("text/html", "application/json", "text/plain") \
            or lowered.startswith(_HTML_PREFIXES):
        raise ArtworkDownloadError("the server returned a web page or error message instead of an image",
                                   permanent=True)
    image_format = detect_format(data)
    if image_format is None:
        raise ArtworkDownloadError("the response is not a supported image (JPEG, PNG, WebP or GIF)", permanent=True)
    return image_format


def default_session() -> requests.Session:
    return requests.Session()


class ArtworkDownloader:
    def __init__(
        self,
        session: Optional[requests.Session] = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 1.0,
        max_bytes: int = 20 * 1024 * 1024,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session or default_session()
        self.timeout = (connect_timeout, read_timeout)
        self.retries = max(0, retries)
        self.backoff = backoff
        self.max_bytes = max_bytes
        self.sleep = sleep
        self.request_count = 0

    @classmethod
    def from_settings(cls, settings, **kwargs) -> "ArtworkDownloader":
        return cls(connect_timeout=min(settings.http_connect_timeout, settings.artwork_timeout_seconds),
                   read_timeout=settings.artwork_timeout_seconds, retries=settings.artwork_retries,
                   max_bytes=settings.artwork_max_size_mb * 1024 * 1024, **kwargs)

    def close(self) -> None:
        self.session.close()

    def _retry_after(self, response) -> float:
        value = (getattr(response, "headers", None) or {}).get("Retry-After")
        if not value:
            return 0.0
        try:
            return min(MAX_RETRY_AFTER, max(0.0, float(value)))
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return 0.0
            return min(MAX_RETRY_AFTER, max(0.0, parsed.timestamp() - time.time())) if parsed else 0.0

    def _read(self, response) -> bytes:
        declared = (response.headers or {}).get("Content-Length")
        if declared and str(declared).isdigit() and int(declared) > self.max_bytes:
            raise ArtworkDownloadError(f"image larger than {self.max_bytes // (1024 * 1024)} MB", permanent=True)
        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > self.max_bytes:
                raise ArtworkDownloadError(f"image larger than {self.max_bytes // (1024 * 1024)} MB", permanent=True)
            chunks.append(chunk)
        return b"".join(chunks)

    def fetch(self, url: str) -> DownloadedImage:
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ArtworkDownloadError("unsupported artwork URL", permanent=True)
        headers = {"User-Agent": f"{APP_NAME}/{__version__}", "Accept": "image/*"}
        attempt = 0
        while True:
            attempt += 1
            wait = self.backoff * (2 ** (attempt - 1))
            response = None
            try:
                self.request_count += 1
                response = self.session.get(url, headers=headers, timeout=self.timeout, stream=True,
                                            allow_redirects=True)
                status = response.status_code
                if status == 200:
                    data = self._read(response)
                    content_type = (response.headers or {}).get("Content-Type", "")
                    return DownloadedImage(data, validate_image(data, content_type))
                if status in (404, 410):
                    raise ArtworkDownloadError(f"image not found (HTTP {status})", permanent=True)
                if status in (401, 403):
                    raise ArtworkDownloadError(f"access denied (HTTP {status})", permanent=True)
                if status == 429:
                    problem = "rate limited (HTTP 429)"
                    wait = max(wait, self._retry_after(response))
                elif 500 <= status < 600:
                    problem = f"server error (HTTP {status})"
                else:
                    raise ArtworkDownloadError(f"download failed (HTTP {status})", permanent=True)
            except requests.Timeout:
                problem = "timed out"
            except requests.RequestException as exc:
                problem = f"connection failed ({type(exc).__name__})"
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:  # pragma: no cover - defensive
                        pass
            if attempt > self.retries:
                raise ArtworkDownloadError(f"{problem} after {attempt} attempt(s)")
            log.debug("Artwork %s: %s; retrying in %.1fs", redact(url), problem, wait)
            self.sleep(wait)
