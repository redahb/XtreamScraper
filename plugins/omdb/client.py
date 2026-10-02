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

from xtreamscraper import APP_NAME, __version__

log = logging.getLogger(__name__)

API_BASE = "https://www.omdbapi.com/"
POSTER_BASE = "https://img.omdbapi.com/"
MAX_POSTER_BYTES = 20 * 1024 * 1024

# "Request limit reached!", "Daily request limit reached!", "API limit reached" ...
_LIMIT = re.compile(r"\b(request|daily|api|rate)\b.*\blimit\b.*\b(reached|exceeded)\b|\blimit (reached|exceeded)\b")
_AUTH = re.compile(r"\b(invalid api ?key|api ?key not provided|no api ?key provided|api ?key (is )?missing)\b")
_NOT_FOUND = re.compile(r"\bnot found\b|\bincorrect imdb id\b")
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
    """Map an OMDb ``Error`` text to an exception. Quota detection is deliberately conservative."""
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
        return {"Accept": accept, "User-Agent": f"{APP_NAME}/{__version__}"}

    # -- transport -----------------------------------------------------------------------------------
    def _get(self, url: str, params: dict[str, Any], accept: str, stream: bool = False, what: str = "OMDb API"):
        """GET with bounded retries for transport failures, 429 and 5xx. Returns the response."""
        attempt = 0
        while True:
            attempt += 1
            wait = self.backoff * (2 ** (attempt - 1))
            try:
                self.request_count += 1
                response = self.session.get(url, params=params, headers=self._headers(accept), timeout=self.timeout,
                                            stream=stream)
            except requests.Timeout:
                problem = f"{what} request timed out"
            except requests.RequestException:
                problem = f"{what} unreachable"
            else:
                status = response.status_code
                if status == 429:
                    problem = f"{what} rate limited (HTTP 429)"
                elif 500 <= status < 600:
                    problem = f"{what} server error (HTTP {status})"
                else:
                    return response
                _close(response)
            if attempt > self.retries:
                raise OMDbError(problem)
            log.debug("%s; retrying in %.1fs (attempt %d)", problem, wait, attempt)
            self.sleep(wait)

    def request(self, params: dict[str, Any]) -> dict[str, Any]:
        """One metadata request; returns the JSON object of a successful (``Response=True``) answer."""
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
        if status == 404:
            raise OMDbNotFound("not found at OMDb")
        if status != 200:
            raise OMDbError(f"OMDb request failed (HTTP {status})")
        if not isinstance(data, dict):
            raise OMDbMalformed("OMDb returned a malformed response")
        if str(data.get("Response", "")).strip().lower() != "true":
            raise OMDbMalformed("OMDb returned an unexpected response")
        return data

    # -- endpoints -----------------------------------------------------------------------------------
    def search(self, title: str, media_type: str, year: Optional[int] = None, page: int = 1) -> dict[str, Any]:
        """``s=`` search; a "not found" answer is an empty result, not an error."""
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
        return self.request({"i": series_id, "Season": int(season), "Episode": int(episode), "plot": "full"})

    def get_season(self, series_id: str, season: int) -> dict[str, Any]:
        return self.request({"i": series_id, "Season": int(season)})

    def get_poster(self, imdb_id: str, height: int) -> PosterImage:
        """High-resolution poster from the patron-only Poster API.

        The request URL carries the API key: it is built here, used once and never
        returned, logged or stored.
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


def _close(response) -> None:
    try:
        response.close()
    except Exception:  # pragma: no cover - defensive
        pass


def _read(response, limit: int) -> bytes:
    iterate = getattr(response, "iter_content", None)
    if iterate is None:
        return bytes(getattr(response, "content", b"") or b"")
    chunks, total = [], 0
    for chunk in iterate(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise OMDbError("OMDb Poster API response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _poster_text(body: bytes, content_type: str) -> Optional[str]:
    """The error text of a non-image Poster API reply (plain text, HTML or JSON), else ``None``."""
    kind = content_type.split(";")[0].strip().lower()
    head = body[:512].lstrip()
    textual = kind.startswith("text/") or kind in ("application/json", "application/xml") \
        or head[:6].lower() in (b"error:", b"<!doct", b"<html>") or head[:1] in (b"{", b"<") \
        or head[:5].lower() == b"<html"
    if not textual:
        return None
    try:
        return body[:2000].decode("utf-8", "replace")
    except Exception:  # pragma: no cover - defensive
        return ""
