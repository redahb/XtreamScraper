"""Artwork downloads: bounded retries, timeouts, size limits and payload validation.

Only a validated image is ever handed back; callers write it atomically. Error messages
go through the general redaction policy, so secrets in query strings never reach logs.

Artwork URLs come from metadata sources, so they are untrusted: a URL (or any redirect hop)
whose host resolves to a private, loopback, link-local, reserved or CGNAT address is refused.
Redirects are followed here, one checked hop at a time, never by ``requests`` itself.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional
from urllib.parse import urljoin, urlsplit

import requests

from ..utils.http_retry import CLOSE_ALWAYS, request_with_retries, user_agent
from ..utils.netguard import Resolver, public_host_problem, system_resolver
from ..utils.redact import redact

if TYPE_CHECKING:
    from ..config.settings import Settings

log = logging.getLogger(__name__)

MAX_RETRY_AFTER = 60.0
MAX_REDIRECTS = 10
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
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
    """Return the image format or raise :class:`ArtworkDownloadError`.

    Contract:
        Preconditions:
            - Only the media type of ``content_type`` counts (parameters after ``;`` are
              ignored, case-insensitive). An empty one is fine: the magic bytes decide.

        Raises:
            ArtworkDownloadError: Always ``permanent``: empty data; a ``text/html``,
                ``application/json`` or ``text/plain`` content type; data whose first 512
                bytes start (after leading whitespace) like HTML, XML or JSON; or no JPEG,
                PNG, WebP or GIF magic bytes.

        Silences:
            - Any other content type (``image/png`` for JPEG bytes,
              ``application/octet-stream``) is ignored; the bytes decide the format.
    """
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


def default_resolver() -> Resolver:
    """The resolver used for the private-address check (tests replace it; no real DNS there)."""
    return system_resolver


def _close(response: requests.Response) -> None:
    try:
        response.close()
    except Exception:  # pragma: no cover - defensive
        pass


def _headers(response: requests.Response) -> Mapping[str, str]:
    """The response headers (an empty mapping when a fake response has none)."""
    return response.headers or {}


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
        resolver: Optional[Resolver] = None,
        allow_private: bool = False,
    ) -> None:
        self.session = session or default_session()
        self.resolver = resolver or default_resolver()
        self.allow_private = allow_private
        self.timeout = (connect_timeout, read_timeout)
        self.retries = max(0, retries)
        self.backoff = backoff
        self.max_bytes = max_bytes
        self.sleep = sleep
        self.request_count = 0

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> ArtworkDownloader:
        return cls(connect_timeout=min(settings.http_connect_timeout, settings.artwork_timeout_seconds),
                   read_timeout=settings.artwork_timeout_seconds, retries=settings.artwork_retries,
                   max_bytes=settings.artwork_max_size_mb * 1024 * 1024, **kwargs)

    def close(self) -> None:
        self.session.close()

    def _read(self, response: requests.Response) -> bytes:
        """The whole body of a streamed response, at most ``max_bytes`` of it.

        Contract:
            Preconditions:
                - ``response`` was requested with ``stream=True``; its body is consumed.

            Raises:
                ArtworkDownloadError: ``permanent`` when the declared ``Content-Length`` or
                    the bytes actually received exceed ``max_bytes`` (reading stops there).
                    The message gives the limit in whole MB (``0 MB`` below 1 MB).
                requests.RequestException: From ``iter_content`` (a broken stream), not
                    caught here; :meth:`fetch` retries it.

            Silences:
                - A ``Content-Length`` that is not all digits is ignored (the received size
                  still counts).
        """
        declared = _headers(response).get("Content-Length")
        if declared and str(declared).isdigit() and int(declared) > self.max_bytes:
            raise ArtworkDownloadError(f"image larger than {self.max_bytes // (1024 * 1024)} MB", permanent=True)
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > self.max_bytes:
                raise ArtworkDownloadError(f"image larger than {self.max_bytes // (1024 * 1024)} MB", permanent=True)
            chunks.append(chunk)
        return b"".join(chunks)

    def _check_target(self, url: str) -> None:
        """Refuse a URL whose host is (or resolves to) a non-public address.

        A refused address is a permanent error; a failed name resolution is a connection
        failure (retried), as it would have been inside ``requests``.

        Contract:
            Raises:
                ArtworkDownloadError: ``permanent`` for a scheme other than http/https, a URL
                    without a host, or a host with any non-public address (logged as a
                    warning with the URL redacted).
                requests.ConnectionError: Name resolution failed (``OSError`` or
                    ``UnicodeError`` from the resolver); :meth:`fetch` retries it.

            Silences:
                - With ``allow_private`` only the scheme is checked.
                - The host is resolved here and again by ``requests``; an answer that changes
                  in between (DNS rebinding) is not caught.
        """
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            raise ArtworkDownloadError("unsupported artwork URL", permanent=True)
        if self.allow_private:
            return
        try:
            problem = public_host_problem(parts.hostname, self.resolver)
        except (OSError, UnicodeError) as exc:
            raise requests.ConnectionError(f"name resolution failed ({type(exc).__name__})") from None
        if problem:
            log.warning("Artwork %s refused: %s", redact(url), problem)
            raise ArtworkDownloadError(f"refused: {problem}", permanent=True)

    def _get(self, url: str, headers: dict[str, str]) -> requests.Response:
        """One attempt: GET ``url``, following redirects manually and checking every hop.

        Redirects must stay manual (``allow_redirects=False``): that is what sends every hop
        through :meth:`_check_target`.

        Contract:
            Raises:
                ArtworkDownloadError: A hop is refused (see :meth:`_check_target`); not retried.
                requests.TooManyRedirects: More than ``MAX_REDIRECTS`` redirects. It is a
                    ``RequestException``, so :meth:`fetch` retries the whole chain.
                requests.RequestException: From ``session.get``; :meth:`fetch` retries it.

            Silences:
                - A redirect status without a ``Location`` header is returned as the final
                  response (:meth:`fetch` then fails with "download failed (HTTP 30x)").
                - Errors closing an intermediate redirect response.
        """
        self.request_count += 1
        target = url
        for _ in range(MAX_REDIRECTS + 1):
            self._check_target(target)
            response = self.session.get(target, headers=headers, timeout=self.timeout, stream=True,
                                        allow_redirects=False)
            location = _headers(response).get("Location") if response.status_code in REDIRECT_STATUSES else None
            if not location:
                return response
            _close(response)
            target = urljoin(target, str(location).strip())
        raise requests.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects")

    def fetch(self, url: str) -> DownloadedImage:
        """Download ``url`` and return the validated image.

        Contract:
            Preconditions:
                - ``url`` must start with ``http://`` or ``https://`` (case-insensitive, after
                  stripping); anything else fails before a request is made.

            Raises:
                ArtworkDownloadError: ``permanent`` for an unsupported URL, a refused host,
                    HTTP 404/410/401/403 or any other non-200 final status (including a
                    redirect without ``Location``), an oversized body or a payload that is not
                    an image (:func:`validate_image`). Not permanent when the retries for a
                    transport error, timeout, too many redirects, HTTP 429 or 5xx ran out
                    (``"... after N attempt(s)"``).

            Silences:
                - Transport errors, 429 and 5xx answers are retried ``retries`` times
                  (exponential ``backoff``, ``Retry-After`` capped at ``MAX_RETRY_AFTER``) and
                  only logged at debug level until the last one.
        """
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ArtworkDownloadError("unsupported artwork URL", permanent=True)
        headers = {"User-Agent": user_agent(), "Accept": "image/*"}

        def final(response: requests.Response) -> DownloadedImage:
            status = response.status_code
            if status == 200:
                data = self._read(response)
                content_type = _headers(response).get("Content-Type", "")
                return DownloadedImage(data, validate_image(data, content_type))
            if status in (404, 410):
                raise ArtworkDownloadError(f"image not found (HTTP {status})", permanent=True)
            if status in (401, 403):
                raise ArtworkDownloadError(f"access denied (HTTP {status})", permanent=True)
            raise ArtworkDownloadError(f"download failed (HTTP {status})", permanent=True)

        return request_with_retries(
            lambda: self._get(url, headers), retries=self.retries, backoff=self.backoff, sleep=self.sleep, on_response=final,
            transport_problem=lambda exc: "timed out" if isinstance(exc, requests.Timeout)
            else f"connection failed ({type(exc).__name__})",
            status_problem=lambda status: "rate limited (HTTP 429)" if status == 429
            else f"server error (HTTP {status})",
            give_up=lambda problem, attempts: ArtworkDownloadError(f"{problem} after {attempts} attempt(s)"),
            max_retry_after=MAX_RETRY_AFTER, close=CLOSE_ALWAYS, logger=log, label=f"Artwork {redact(url)}")
