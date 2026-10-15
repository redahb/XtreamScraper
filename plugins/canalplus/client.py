"""Minimal Canal+ HTTP client (a country website's public Algolia search index, no account).

* **Search settings:** the country website's stream page (:attr:`~.sites.Site.settings_url`, e.g.
  ``https://www.canalplus.nl/stream/``) carries the public Algolia application ID and
  *search-only* API key in an inline ``aa('init', {...})`` call. They are read at run time (never
  hard-coded), cached per website for the whole process behind a lock and shared by all sessions. The
  key is shipped to every browser, but it is still registered with
  :func:`~xtreamscraper.utils.redact.register_secret` so it never reaches a log, an error or the
  database.
* **Failed discovery** is remembered for :data:`DISCOVERY_COOLDOWN` seconds only (no request storm,
  no permanent poisoning): a later job tries again.
* **401/403:** the cached settings are dropped, read again from the website and the request is
  retried exactly once. A second rejection is :class:`CanalPlusClientConfigError` ("Canal+ search
  key rejected").
* **Credentials travel in headers only** (``X-Algolia-API-Key``, ``X-Algolia-Application-Id``),
  never in a URL.
* Connection/read timeouts; bounded retries with backoff for timeouts, connection errors, 429
  (honouring ``Retry-After``) and 5xx. Other statuses are never retried. A final 429 is a
  :class:`CanalPlusError` with ``status`` 429; the session then suspends the plugin for the job.
* At most :data:`MAX_CONCURRENT_REQUESTS` Canal+ requests run at the same time, over all sessions.

Nothing here logs the key, a request header map, a request URL or the website's inline snippet.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote

import requests

from xtreamscraper.utils.http_retry import request_with_retries, user_agent
from xtreamscraper.utils.redact import register_secret

from .sites import NL, SITES, Site

log = logging.getLogger(__name__)

#: The Dutch website's settings page and index (the default site).
SITE_URL = NL.settings_url
INDEX = NL.index
MAX_RETRY_AFTER = 30.0
MAX_CONCURRENT_REQUESTS = 2
#: A failed search-settings discovery is not retried for this many seconds.
DISCOVERY_COOLDOWN = 5 * 60.0
#: Statuses Algolia uses for a rejected application ID or key.
UNAUTHORIZED_STATUSES = frozenset({401, 403})
#: Algolia's maximum query length is 512 bytes; longer local titles are cut.
MAX_QUERY_LENGTH = 200
_GATE = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)

OUTDATED_MESSAGE = ("Canal+ search settings could not be read from the website; the plugin is probably "
                    "outdated")
REJECTED_MESSAGE = "Canal+ search key rejected"


class CanalPlusError(Exception):
    """Transient or unexpected failure; the message is safe to show.

    ``status`` is the HTTP status of a final non-2xx answer (429 after the retries included),
    else ``None``.
    """

    def __init__(self, message: str = "", status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class CanalPlusNotFound(CanalPlusError):
    """Canal+ says the requested object does not exist (HTTP 404)."""


class CanalPlusFormatError(CanalPlusError):
    """Canal+ answered, but not in a format this plugin understands."""


class CanalPlusAuthError(CanalPlusError):
    """No usable search settings: nothing can be searched for the rest of the job."""


class CanalPlusClientConfigError(CanalPlusAuthError):
    """The website's search settings are missing or malformed, or Algolia rejected them twice.

    The plugin is then probably outdated; it is never a "not found".
    """


class _Unauthorized(Exception):
    """Internal: HTTP 401/403 on an Algolia request."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


# -- search settings ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Credentials:
    """The public Algolia application ID and search-only key (the key is never in ``repr``)."""

    app_id: str
    api_key: str = field(repr=False)

    @property
    def host(self) -> str:
        return f"https://{self.app_id.lower()}-dsn.algolia.net"


#: The ``aa('init', {...})`` call: a small object without nested braces.
_INIT = re.compile(r"""\baa\(\s*(['"])init\1\s*,\s*\{([^{}]{1,600})\}\s*\)""")
_APP_ID = re.compile(r"[A-Za-z0-9]{4,64}")
_API_KEY = re.compile(r"[A-Za-z0-9_\-]{8,200}")


def _field(region: str, name: str) -> Optional[str]:
    """The quoted string value of ``name`` (a bare or quoted key) in an object literal's body."""
    pattern = re.compile(r"""(?:^|[\s,])(['"]?)""" + name + r"""\1\s*:\s*(['"])([^'"\\\n]{0,300})\2""")
    found = {match.group(3).strip() for match in pattern.finditer(region)}
    return found.pop() if len(found) == 1 else None


def parse_credentials(html: str) -> Credentials:
    """The application ID and search-only key from the website's ``aa('init', {...})`` call.

    Field order, quote style and whitespace do not matter, and the key is not assumed to be
    hexadecimal. Nothing is evaluated: both fields are read independently with bounded patterns.

    Contract:
        Raises:
            CanalPlusClientConfigError: no such call, a field missing, given twice with
                different values, or not a sane value (:data:`OUTDATED_MESSAGE`).
    """
    for match in _INIT.finditer(html if isinstance(html, str) else ""):
        region = match.group(2)
        app_id, api_key = _field(region, "appId"), _field(region, "apiKey")
        if app_id and api_key and _APP_ID.fullmatch(app_id) and _API_KEY.fullmatch(api_key):
            return Credentials(app_id, api_key)
    raise CanalPlusClientConfigError(OUTDATED_MESSAGE)


class CredentialCache:
    """Search settings shared by every Canal+ session of the process.

    A positive result stays until Algolia rejects it; a failure is remembered for
    :data:`DISCOVERY_COOLDOWN` seconds only.
    """

    def __init__(self, cooldown: float = DISCOVERY_COOLDOWN) -> None:
        self.cooldown = cooldown
        self._lock = threading.Lock()
        self._credentials: Optional[Credentials] = None
        self._failure: Optional[CanalPlusAuthError] = None
        self._failed_at = 0.0

    def get(self, discover: Callable[[], Credentials], clock: Callable[[], float]) -> Credentials:
        """The cached settings, discovered (once, under the lock) when there are none.

        Contract:
            Raises:
                CanalPlusAuthError: discovery failed now, or less than ``cooldown`` seconds ago
                    (then without a request). Any other exception of ``discover`` propagates and
                    is not remembered.
        """
        with self._lock:
            if self._credentials is not None:
                return self._credentials
            if self._failure is not None and clock() - self._failed_at < self.cooldown:
                raise self._failure
            try:
                credentials = discover()
            except CanalPlusAuthError as exc:
                self._failure, self._failed_at = exc, clock()
                raise
            register_secret(credentials.api_key)
            self._credentials, self._failure = credentials, None
            return credentials

    def invalidate(self, credentials: Credentials) -> None:
        """Drop ``credentials`` (when still cached) so the next call discovers them again."""
        with self._lock:
            if self._credentials == credentials:
                self._credentials = None

    def clear(self) -> None:
        with self._lock:
            self._credentials, self._failure, self._failed_at = None, None, 0.0


#: The process-wide caches used by default, one per website.
SITE_CREDENTIALS = {site.tag: CredentialCache() for site in SITES}
CREDENTIALS = SITE_CREDENTIALS[NL.tag]


# -- search answers -------------------------------------------------------------------------------
def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass
class SearchPage:
    """One page of an Algolia search answer: the object hits and the pagination figures.

    ``malformed`` counts entries of ``hits`` that are not objects with string ``objectID``,
    ``title``, ``url`` and ``type`` (they are left out of ``hits``).
    """

    hits: list[dict[str, Any]]
    page: Optional[int] = None
    nb_pages: Optional[int] = None
    nb_hits: Optional[int] = None
    malformed: int = 0


def usable_hit(hit: Any) -> bool:
    """An object with non-empty string ``objectID``, ``title``, ``url`` and ``type``."""
    return isinstance(hit, dict) and all(isinstance(hit.get(key), str) and hit[key].strip()
                                         for key in ("objectID", "title", "url", "type"))


def parse_search(data: Any) -> SearchPage:
    """Validate a search answer.

    Contract:
        Raises:
            CanalPlusFormatError: not an object, no ``hits`` list, or pagination that cannot be
                (hits on a ``page`` at or beyond ``nbPages``; an empty page there is valid).
        Silences:
            - Malformed individual hits are skipped (counted in ``malformed`` and a debug log
              line). Missing or odd ``page``/``nbPages``/``nbHits`` become ``None``.
    """
    if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
        raise CanalPlusFormatError("Canal+ search answered in an unexpected format")
    hits = [hit for hit in data["hits"] if usable_hit(hit)]
    malformed = len(data["hits"]) - len(hits)
    if malformed:
        log.debug("Canal+ search: %d malformed hit(s) skipped", malformed)
    page, nb_pages = _count(data.get("page")), _count(data.get("nbPages"))
    if hits and page is not None and nb_pages is not None and page >= nb_pages:
        raise CanalPlusFormatError("Canal+ search pagination is inconsistent")
    return SearchPage(hits, page, nb_pages, _count(data.get("nbHits")), malformed)


# -- client ---------------------------------------------------------------------------------------
class CanalPlusClient:
    session: requests.Session
    credentials: CredentialCache
    site: Site
    timeout: tuple[float, float]
    retries: int
    backoff: float
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    request_count: int

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        credentials: Optional[CredentialCache] = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        site: Site = NL,
    ) -> None:
        self.session = session or requests.Session()
        self.site = site
        self.credentials = credentials if credentials is not None else SITE_CREDENTIALS[site.tag]
        self.timeout = (connect_timeout, read_timeout)
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep
        self.clock = clock
        self.request_count = 0

    def close(self) -> None:
        self.session.close()

    # -- transport ---------------------------------------------------------------------------------
    def _send(self, method: str, url: str, context: str, headers: dict[str, str],
              body: Optional[dict[str, Any]] = None, unauthorized: bool = False) -> requests.Response:
        """Send one request with bounded retries and return the 2xx response.

        Contract:
            Preconditions:
                - ``context`` is a short, secret-free label: it appears in every error message.
            Raises:
                _Unauthorized: HTTP 401/403 when ``unauthorized`` is true (never retried).
                CanalPlusNotFound: HTTP 404 (never retried).
                CanalPlusError: any other non-2xx status outside 429/5xx (not retried); and, once
                    the retries are exhausted, 429 (``status`` 429), 5xx, timeouts and
                    connection errors.
        """
        all_headers = {"User-Agent": user_agent(), **headers}
        last_status: list[Optional[int]] = [None]

        def send() -> requests.Response:
            self.request_count += 1
            last_status[0] = None
            with _GATE:
                if body is None:
                    response = self.session.request(method, url, headers=all_headers, timeout=self.timeout)
                else:
                    response = self.session.request(method, url, headers=all_headers, json=body,
                                                    timeout=self.timeout)
            last_status[0] = response.status_code
            return response

        def final(response: requests.Response) -> requests.Response:
            status = response.status_code
            if 200 <= status < 300:
                return response
            if unauthorized and status in UNAUTHORIZED_STATUSES:
                raise _Unauthorized(status)
            if status == 404:
                raise CanalPlusNotFound(f"Not found at Canal+ ({context})", status)
            raise CanalPlusError(f"Canal+ {context} request failed (HTTP {status})", status)

        def give_up(problem: str, attempts: int) -> CanalPlusError:
            return CanalPlusError(problem, 429 if last_status[0] == 429 else None)

        return request_with_retries(
            send, retries=self.retries, backoff=self.backoff, sleep=self.sleep, on_response=final,
            transport_problem=lambda exc: f"Canal+ {context} request timed out" if isinstance(exc, requests.Timeout)
            else "Canal+ unreachable",
            status_problem=lambda status: "Canal+ rate limit reached" if status == 429
            else f"Canal+ server error (HTTP {status}, {context})",
            give_up=give_up, max_retry_after=MAX_RETRY_AFTER, logger=log, label=f"Canal+ {context}")

    @staticmethod
    def _json_object(response: requests.Response, context: str) -> dict[str, Any]:
        """The response body as a JSON object.

        Contract:
            Raises:
                CanalPlusFormatError: the body is not valid JSON, or not a JSON object.
        """
        try:
            data = json.loads(response.content or b"")
        except ValueError:
            raise CanalPlusFormatError(f"Canal+ returned a malformed response for {context}") from None
        if not isinstance(data, dict):
            raise CanalPlusFormatError(f"Canal+ returned an unexpected response for {context}")
        return data

    # -- search settings ---------------------------------------------------------------------------
    def _discover(self) -> Credentials:
        """Read the search settings from the website.

        Contract:
            Raises:
                CanalPlusClientConfigError: the page answered 404 or another 4xx (but 429), or
                    holds no usable ``aa('init', ...)`` call.
                CanalPlusAuthError: the page could not be loaded (network, 5xx, 429).
        """
        try:
            response = self._send("GET", self.site.settings_url, "website", {"Accept": "text/html"})
        except CanalPlusError as exc:
            if exc.status is not None and 400 <= exc.status < 500 and exc.status != 429:
                log.warning("Canal+ website answered HTTP %d instead of the search settings", exc.status)
                raise CanalPlusClientConfigError(OUTDATED_MESSAGE, exc.status) from None
            raise CanalPlusAuthError(f"Canal+ search settings could not be loaded ({exc})", exc.status) from None
        html = (response.content or b"").decode("utf-8", errors="replace")
        try:
            return parse_credentials(html)
        except CanalPlusClientConfigError:
            log.warning("Canal+ website holds no readable search settings (page of %d characters)", len(html))
            raise

    def search_credentials(self) -> Credentials:
        """The shared search settings (see :class:`CredentialCache`)."""
        return self.credentials.get(self._discover, self.clock)

    def _algolia(self, method: str, path: str, context: str, body: Optional[dict[str, Any]] = None
                 ) -> dict[str, Any]:
        """One Algolia request on the site's index; on 401/403 rediscover the settings and retry once.

        Contract:
            Raises:
                CanalPlusClientConfigError: rejected twice (:data:`REJECTED_MESSAGE`), or no
                    usable settings on the website.
                CanalPlusAuthError: the settings could not be loaded.
                CanalPlusNotFound, CanalPlusFormatError, CanalPlusError: from the request/body.
        """
        for attempt in (1, 2):
            credentials = self.search_credentials()
            headers = {"Accept": "application/json", "X-Algolia-API-Key": credentials.api_key,
                       "X-Algolia-Application-Id": credentials.app_id}
            url = f"{credentials.host}/1/indexes/{self.site.index}{path}"
            try:
                response = self._send(method, url, context, headers, body, unauthorized=True)
            except _Unauthorized as exc:
                self.credentials.invalidate(credentials)
                if attempt == 1:
                    log.info("Canal+ %s: search key rejected (HTTP %d), reading the website's settings again",
                             context, exc.status)
                    continue
                log.warning("Canal+ %s: the rediscovered search key was rejected too (HTTP %d)", context,
                            exc.status)
                raise CanalPlusClientConfigError(REJECTED_MESSAGE, exc.status) from None
            return self._json_object(response, context)
        raise AssertionError("unreachable")  # pragma: no cover

    # -- endpoints ---------------------------------------------------------------------------------
    def search(self, query: str, filters: str, page: int = 0, hits_per_page: int = 50) -> SearchPage:
        """One page of a title search (``restrictSearchableAttributes=["title"]``)."""
        body = {"query": (query or "")[:MAX_QUERY_LENGTH], "filters": filters, "page": int(page),
                "hitsPerPage": int(hits_per_page), "restrictSearchableAttributes": ["title"]}
        return parse_search(self._algolia("POST", "/query", "search", body))

    def get_object(self, object_id: str) -> dict[str, Any]:
        """One record by ``objectID`` (URL-quoted as one path segment, so it cannot change the route).

        Contract:
            Raises:
                CanalPlusNotFound: HTTP 404.
                CanalPlusFormatError: not an object with string ``objectID``/``title``/``url``/``type``.
        """
        data = self._algolia("GET", "/" + quote(str(object_id), safe=""), "title record")
        if not usable_hit(data):
            raise CanalPlusFormatError("Canal+ title record answered in an unexpected format")
        return data
