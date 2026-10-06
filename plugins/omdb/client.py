"""Minimal OMDb API client (official HTTP API and Poster API only).

* Every metadata request asks for JSON (``r=json``); JSONP is never used.
* OMDb reports logical errors inside HTTP 200 responses (``{"Response": "False",
  "Error": "..."}``), so every response is inspected and classified: not found,
  authentication failure, request limit reached, or another failure.
* Connection/read timeouts, connection reuse, bounded retries with backoff for timeouts,
  connection errors, 429 and 5xx. Logical errors are never retried.
* Query strings are built by ``requests`` from a parameter dict; titles are never
  concatenated into URLs. Error messages never contain URLs or the API key.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import requests

from xtreamscraper.utils.http_retry import CLOSE_RETRIED, request_with_retries, user_agent

log = logging.getLogger(__name__)

API_BASE = "https://www.omdbapi.com/"
POSTER_BASE = "https://img.omdbapi.com/"
MAX_POSTER_BYTES = 20 * 1024 * 1024

# "Request limit reached!", "Daily request limit reached!", "API limit reached" ...
_LIMIT = re.compile(r"\b(request|daily|api|rate)\b.*\blimit\b.*\b(reached|exceeded)\b|\blimit (reached|exceeded)\b")
_AUTH = re.compile(r"\b(invalid api ?key|api ?key not provided|no api ?key provided|api ?key (is )?missing)\b")
# Only OMDb's known "no such title" answers: any other text (even one containing "not found",
# such as "API key not found!") is an API error, which keeps bindings.
_NOT_FOUND = re.compile(r"\b(movie|series|episode|series or episode) not found\b|\bincorrect imdb id\b")
_TOO_MANY = re.compile(r"\btoo many results\b")


class OMDbError(Exception):
    """Transient or unexpected failure; the message is safe to show."""


class OMDbAuthError(OMDbError):
    """The API key is missing or invalid (session-wide)."""


class OMDbQuotaError(OMDbError):
    """OMDb reports that the key's request limit has been reached (session-wide)."""


class OMDbNotFound(OMDbError):
    """OMDb definitively reports that the requested title/episode does not exist."""


class OMDbMalformed(OMDbError):
    """The response was not the JSON object OMDb documents."""


class PosterUnavailable(OMDbError):
    """The Poster API refused this key (not a patron key) - the metadata API is unaffected."""


class PosterNotFound(OMDbError):
    """The Poster API has no poster for this IMDb ID."""


def _norm(text: str) -> str:
    return " ".join((text or "").casefold().split())


def classify_error(message: str) -> OMDbError:
    """Map an OMDb ``Error`` text to an exception. Quota detection is deliberately conservative.

    The text is case- and whitespace-normalized, then checked in a fixed order: request limit,
    API key, OMDb's known "no such title" answers, "too many results". The exception is
    returned, not raised.

    Contract:
        Preconditions:
            - ``message`` is OMDb's free-text ``Error`` value; ``None`` or ``""`` is accepted
              and gives a plain :class:`OMDbError`.

        Silences:
            - Any text that matches none of the known patterns becomes a plain
              :class:`OMDbError` ("OMDb request failed"): an API error that keeps bindings.
              A new quota wording is therefore not detected as quota (no job suspension).
            - "Too many results." becomes :class:`OMDbNotFound`, which ``search`` turns into
              an empty result.
    """
    text = _norm(message)
    if _LIMIT.search(text):
        return OMDbQuotaError("OMDb request limit reached")
    if _AUTH.search(text):
        return OMDbAuthError("OMDb API key invalid" if "invalid" in text else "OMDb API key missing")
    if _NOT_FOUND.search(text):
        return OMDbNotFound("not found at OMDb")
    if _TOO_MANY.search(text):
        return OMDbNotFound("too many results at OMDb for this title")
    return OMDbError("OMDb request failed")


@dataclass
class PosterImage:
    data: bytes
    content_type: str


class OMDbClient:
    """One OMDb connection (metadata API and Poster API) for a scrape session or a test.

    Contract:
        Preconditions:
            - ``request_count`` is not locked; it is a diagnostic counter only.

        Raises:
            OMDbAuthError: From ``__init__`` when ``api_key`` is blank (explicit; nothing is
                requested).
    """

    api_key: str
    base_url: str
    poster_base_url: str
    session: requests.Session
    timeout: tuple[float, float]
    retries: int
    backoff: float
    sleep: Callable[[float], None]
    request_count: int

    def __init__(
        self,
        api_key: str,
        base_url: str = API_BASE,
        poster_base_url: str = POSTER_BASE,
        session: Optional[requests.Session] = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        retries: int = 2,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not (api_key or "").strip():
            raise OMDbAuthError("OMDb API key missing")
        self.api_key = api_key.strip()
        self.base_url = base_url or API_BASE
        self.poster_base_url = poster_base_url or POSTER_BASE
        self.session = session or requests.Session()
        self.timeout = (connect_timeout, read_timeout)
        self.retries = max(0, retries)
        self.backoff = backoff
        self.sleep = sleep
        self.request_count = 0  # diagnostics only; never treated as quota state

    def close(self) -> None:
        self.session.close()

    def _headers(self, accept: str) -> dict[str, str]:
        return {"Accept": accept, "User-Agent": user_agent()}

    # -- transport -----------------------------------------------------------------------------------
    def _get(self, url: str, params: dict[str, Any], accept: str, stream: bool = False,
             what: str = "OMDb API") -> requests.Response:
        """GET with bounded retries for transport failures, 429 and 5xx. Returns the response.

        Contract:
            Raises:
                OMDbError: After ``retries`` retries of a timeout, connection error, HTTP 429 or
                    5xx (from ``request_with_retries``' ``give_up``); the message names ``what``,
                    never the URL or the key. ``Retry-After`` is ignored.

            Silences:
                - Every other status (including 401/403/404) is returned to the caller unchecked.
        """
        def send() -> requests.Response:
            self.request_count += 1
            return self.session.get(url, params=params, headers=self._headers(accept), timeout=self.timeout,
                                    stream=stream)

        return request_with_retries(
            send, retries=self.retries, backoff=self.backoff, sleep=self.sleep, on_response=lambda response: response,
            transport_problem=lambda exc: f"{what} request timed out" if isinstance(exc, requests.Timeout)
            else f"{what} unreachable",
            status_problem=lambda status: f"{what} rate limited (HTTP 429)" if status == 429
            else f"{what} server error (HTTP {status})",
            give_up=lambda problem, attempts: OMDbError(problem),
            retry_after_statuses=(), close=CLOSE_RETRIED, logger=log)

    def request(self, params: dict[str, Any]) -> dict[str, Any]:
        """One metadata request; returns the JSON object of a successful (``Response=True``) answer.

        Parameters whose value is ``None`` or ``""`` are dropped; ``r=json`` and ``apikey`` are
        always set (they override caller values).

        Contract:
            Raises:
                OMDbQuotaError, OMDbAuthError, OMDbNotFound, OMDbError: OMDb's in-band
                    ``Response: False`` answer, classified by :func:`classify_error`, whatever
                    the HTTP status.
                OMDbAuthError: HTTP 401 or 403 without an in-band error (every 403 is read as a
                    rejected key, also a proxy's).
                OMDbError: Any other non-200 status, including a bare 404 (a wrong base URL,
                    never "not found"), and transport failures after the retries (from ``_get``).
                OMDbMalformed: HTTP 200 whose body is not a JSON object, or whose ``Response``
                    is not ``"True"``.

            Silences:
                - ``ValueError`` from ``response.json()``: the body is then treated as non-JSON
                  (malformed on HTTP 200, the status error otherwise).
        """
        query = {k: v for k, v in params.items() if v not in (None, "")}
        query.update(r="json", apikey=self.api_key)
        response = self._get(self.base_url, query, "application/json")
        status = response.status_code
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and str(data.get("Response", "")).strip().lower() == "false":
            raise classify_error(str(data.get("Error") or ""))
        if status == 401:
            raise OMDbAuthError("OMDb API key invalid")
        if status == 403:
            raise OMDbAuthError("Access denied by OMDb (check the API key)")
        if status != 200:
            # Including 404: OMDb reports unknown titles in-band (above), so a bare 404 means a
            # wrong base URL or a proxy page - an API error that keeps bindings, never "not found".
            raise OMDbError(f"OMDb request failed (HTTP {status})")
        if not isinstance(data, dict):
            raise OMDbMalformed("OMDb returned a malformed response")
        if str(data.get("Response", "")).strip().lower() != "true":
            raise OMDbMalformed("OMDb returned an unexpected response")
        return data

    # -- endpoints -----------------------------------------------------------------------------------
    def search(self, title: str, media_type: str, year: Optional[int] = None, page: int = 1) -> dict[str, Any]:
        """``s=`` search; a "not found" answer is an empty result, not an error.

        Contract:
            Raises:
                OMDbError: Every error of :meth:`request` except :class:`OMDbNotFound`.

            Silences:
                - :class:`OMDbNotFound` (including "Too many results.") becomes
                  ``{"Search": [], "totalResults": "0"}``; the caller cannot tell it from a real
                  empty answer.
        """
        try:
            return self.request({"s": title, "type": media_type, "y": year, "page": page if page > 1 else None})
        except OMDbNotFound:
            return {"Search": [], "totalResults": "0"}

    def get_by_id(self, imdb_id: str) -> dict[str, Any]:
        return self.request({"i": imdb_id, "plot": "full"})

    def get_by_title(self, title: str, media_type: str, year: Optional[int] = None) -> dict[str, Any]:
        """``t=`` lookup (OMDb picks one title). Not used for matching; see the plugin."""
        return self.request({"t": title, "type": media_type, "y": year, "plot": "full"})

    def get_episode(self, series_id: str, season: int, episode: int) -> dict[str, Any]:
        """``i=<series>&Season=&Episode=``. Raises what :meth:`request` raises; ``season`` and
        ``episode`` go through ``int()`` (``ValueError``/``TypeError`` for non-numbers)."""
        return self.request({"i": series_id, "Season": int(season), "Episode": int(episode), "plot": "full"})

    def get_season(self, series_id: str, season: int) -> dict[str, Any]:
        return self.request({"i": series_id, "Season": int(season)})

    def get_poster(self, imdb_id: str, height: int) -> PosterImage:
        """High-resolution poster from the patron-only Poster API.

        The request URL carries the API key: it is built here, used once and never
        returned, logged or stored.

        Contract:
            Preconditions:
                - ``height`` must be convertible by ``int()`` (``ValueError``/``TypeError``
                  otherwise); it is not range-checked here (the plugin clamps it to 1-3000).

            Raises:
                OMDbAuthError, OMDbQuotaError: A textual reply (any status) whose text names the
                    key or the request limit. The Poster API rejects a non-patron key with the same
                    "Invalid API key!" text as an unknown key, so ``OMDbAuthError`` here does not
                    prove the key is invalid for the metadata API.
                PosterNotFound: HTTP 404, or a textual reply with OMDb's known not-found text.
                PosterUnavailable: HTTP 401/403 with a binary or unrecognized textual body.
                OMDbError: Other textual replies on any status; other non-200 statuses; an empty
                    body; a body over ``MAX_POSTER_BYTES``; transport failures (from ``_get`` after
                    retries, or from ``_read`` while streaming).

            Silences:
                - Errors from ``response.close()`` (``_close``).
                - A non-textual HTTP 200 body is returned as is, whatever its bytes: the caller
                  must validate the image (``validate_image``).
        """
        params = {"i": imdb_id, "h": int(height), "apikey": self.api_key}
        response = self._get(self.poster_base_url, params, "image/*", stream=True, what="OMDb Poster API")
        try:
            status = response.status_code
            content_type = (response.headers or {}).get("Content-Type", "") or ""
            body = _read(response, MAX_POSTER_BYTES)
        finally:
            _close(response)
        text = _poster_text(body, content_type)
        if text is not None:
            error = classify_error(text)
            if isinstance(error, (OMDbAuthError, OMDbQuotaError)):
                raise error
            if isinstance(error, OMDbNotFound) or status == 404:
                raise PosterNotFound("no poster at the OMDb Poster API")
            if status in (401, 403):
                raise PosterUnavailable("Poster API unavailable for this key")
            raise OMDbError("OMDb Poster API returned an error instead of an image")
        if status in (401, 403):
            raise PosterUnavailable("Poster API unavailable for this key")
        if status == 404:
            raise PosterNotFound("no poster at the OMDb Poster API")
        if status != 200:
            raise OMDbError(f"OMDb Poster API request failed (HTTP {status})")
        if not body:
            raise OMDbError("OMDb Poster API returned an empty response")
        return PosterImage(body, content_type)


def _close(response: requests.Response) -> None:
    """Close a response; any error from ``close()`` is silenced (cleanup must not mask the result)."""
    try:
        response.close()
    except Exception:  # pragma: no cover - defensive
        pass


def _read(response: requests.Response, limit: int) -> bytes:
    """The streamed body, at most ``limit`` bytes. Transport failures while reading (a reset
    connection, a truncated chunked body...) become :class:`OMDbError`.

    Contract:
        Raises:
            OMDbError: When more than ``limit`` bytes arrive (explicit; reading stops there), or
                a ``requests.RequestException`` occurs while reading (only the exception's class
                name is kept, never its text, which may hold the key-bearing URL).

        Silences:
            - A response without ``iter_content`` falls back to ``.content`` (not size-capped).
    """
    try:
        iterate = getattr(response, "iter_content", None)
        if iterate is None:
            return bytes(getattr(response, "content", b"") or b"")
        chunks: list[bytes] = []
        total = 0
        for chunk in iterate(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise OMDbError("OMDb Poster API response is too large")
            chunks.append(chunk)
        return b"".join(chunks)
    except requests.RequestException as exc:
        raise OMDbError(f"OMDb Poster API download failed ({type(exc).__name__})") from None


def _poster_text(body: bytes, content_type: str) -> Optional[str]:
    """The error text of a non-image Poster API reply (plain text, HTML or JSON), else ``None``.

    Textual means a ``text/*``, JSON or XML content type, or a body starting with ``Error:``,
    ``{`` or ``<``, whatever the declared type. Only the first 2000 bytes are decoded; invalid
    UTF-8 becomes U+FFFD.
    """
    kind = content_type.split(";")[0].strip().lower()
    head = body[:512].lstrip()
    textual = kind.startswith("text/") or kind in ("application/json", "application/xml") \
        or head[:6].lower() in (b"error:", b"<!doct", b"<html>") or head[:1] in (b"{", b"<") \
        or head[:5].lower() == b"<html"
    if not textual:
        return None
    return body[:2000].decode("utf-8", "replace")  # "replace" never raises
