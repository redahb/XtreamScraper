"""Minimal TMDB API v3 client (documented API only).

* Authentication: the API Read Access Token as a Bearer header when configured,
  otherwise the v3 API key as ``api_key`` query parameter.
* Connection/read timeouts, connection reuse, bounded concurrency.
* Bounded retries with backoff for timeouts, connection errors, 429 (honouring
  ``Retry-After``) and 5xx. 401/403/404 are never retried.
* Error messages never contain request URLs or credentials.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Optional

import requests

from xtreamscraper.utils.http_retry import request_with_retries, user_agent

log = logging.getLogger(__name__)

API_BASE = "https://api.themoviedb.org/3"
MAX_RETRY_AFTER = 30.0
#: TMDB's own IDs are numbers; they come from NFO files and bindings and go into URL paths.
_TMDB_ID = re.compile(r"[0-9]{1,12}")
#: External IDs for /find (IMDb tt..., TVDB digits, Wikidata Q...): letters and digits only.
_EXTERNAL_ID = re.compile(r"[A-Za-z0-9]{1,32}")


class TMDBError(Exception):
    """Transient or unexpected failure; the message is safe to show."""


class TMDBAuthError(TMDBError):
    pass


class TMDBNotFound(TMDBError):
    pass


class TMDBInvalidId(TMDBNotFound):
    """The ID cannot be a TMDB (or external) ID; nothing was requested."""


class TMDBFormatError(TMDBError):
    """TMDB answered HTTP 200, but not with the JSON object it documents."""


def _checked(value: object, pattern: "re.Pattern[str]", what: str) -> str:
    """``str(value)`` when it fully matches ``pattern``; the guard before an ID enters a URL path.

    Contract:
        Raises:
            TMDBInvalidId: The text does not fully match (explicit). The message quotes at most
                40 characters of the value. Being a ``TMDBNotFound``, callers report it as
                "not found" without a request having been made.
    """
    text = str(value)
    if not pattern.fullmatch(text):
        raise TMDBInvalidId(f"{text[:40]!r} is not a valid {what}")
    return text


def _decode(response: requests.Response) -> dict[str, Any]:
    """A final (non-retried) TMDB response: the JSON object, or the matching error.

    Contract:
        Preconditions:
            - Called only for statuses the retry loop does not retry (not 429 or 5xx).

        Raises:
            TMDBFormatError: HTTP 200 whose body is not JSON (``ValueError`` from ``json()``,
                which includes ``requests.JSONDecodeError``) or not a JSON object.
            TMDBAuthError: HTTP 401 or 403. Every 403 is read as a rejected credential, also one
                from a proxy or firewall in front of TMDB.
            TMDBNotFound: HTTP 404.
            TMDBError: Any other status (e.g. 400, 3xx), with the status in the message.
    """
    status = response.status_code
    if status == 200:
        try:
            data = response.json()
        except ValueError:
            raise TMDBFormatError("TMDB returned a malformed response") from None
        if not isinstance(data, dict):
            raise TMDBFormatError("TMDB returned an unexpected response")
        return data
    if status == 401:
        raise TMDBAuthError("Authentication failed")
    if status == 403:
        raise TMDBAuthError("Access denied by TMDB (check the API credential)")
    if status == 404:
        raise TMDBNotFound("Not found at TMDB")
    raise TMDBError(f"TMDB request failed (HTTP {status})")


def _search_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The result objects of a search answer; a missing or non-list ``results`` is a changed format."""
    results = data.get("results")
    if not isinstance(results, list):
        raise TMDBFormatError("TMDB returned an unexpected search response")
    return [r for r in results if isinstance(r, dict)]


class TMDBClient:
    """One TMDB API connection (one per scrape session or connection test).

    Contract:
        Preconditions:
            - Concurrency is bounded by ``max_concurrency`` (at least 1), but ``request_count``
              and the configuration cache are not locked: share a client between threads only
              if a slightly low count and a duplicate ``/configuration`` request are acceptable.

        Raises:
            TMDBAuthError: From ``__init__`` when neither ``api_key`` nor ``read_token`` is set
                (explicit; nothing is requested).
    """

    api_key: str
    read_token: str
    session: requests.Session
    timeout: tuple[float, float]
    retries: int
    backoff: float
    sleep: Callable[[float], None]
    request_count: int

    def __init__(
        self,
        api_key: str = "",
        read_token: str = "",
        session: Optional[requests.Session] = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 1.0,
        max_concurrency: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key and not read_token:
            raise TMDBAuthError("No TMDB API key or read access token configured")
        self.api_key = api_key
        self.read_token = read_token
        self.session = session or requests.Session()
        self.timeout = (connect_timeout, read_timeout)
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep
        self._gate = threading.BoundedSemaphore(max(1, max_concurrency))
        self._config: Optional[dict[str, Any]] = None
        self.request_count = 0

    def close(self) -> None:
        self.session.close()

    # -- transport ---------------------------------------------------------------------------------
    def _auth(self, params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        headers = {"Accept": "application/json", "User-Agent": user_agent()}
        if self.read_token:
            headers["Authorization"] = f"Bearer {self.read_token}"
        else:
            params = {**params, "api_key": self.api_key}
        return params, headers

    def request(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """GET ``path`` and return the decoded JSON object.

        ``None`` and ``""`` parameter values are dropped; the credential is added as a Bearer
        header (read token) or ``api_key`` parameter.

        Contract:
            Preconditions:
                - ``path`` starts with ``/`` and is already safe for a URL path; the endpoint
                  methods check IDs with ``_checked`` first. ``request`` itself does not.

            Raises:
                TMDBAuthError, TMDBNotFound, TMDBFormatError: From ``_decode`` on the final
                    response (never retried).
                TMDBError: 429/5xx answers or transport failures (timeout, connection error) that
                    are still failing after ``retries`` retries ("TMDB rate limit reached",
                    "TMDB server error (HTTP n)", "TMDB API request timed out", "TMDB API
                    unreachable"). No message contains the URL or the credential.

            Silences:
                - Failed attempts before the last one (logged at DEBUG, then retried after
                  ``self.sleep``; a 429 waits at least its ``Retry-After``, capped at
                  ``MAX_RETRY_AFTER``).
        """
        query, headers = self._auth({k: v for k, v in (params or {}).items() if v not in (None, "")})
        url = API_BASE + path

        def send() -> requests.Response:
            with self._gate:
                self.request_count += 1
                return self.session.get(url, params=query, headers=headers, timeout=self.timeout)

        return request_with_retries(
            send, retries=self.retries, backoff=self.backoff, sleep=self.sleep, on_response=_decode,
            transport_problem=lambda exc: "TMDB API request timed out" if isinstance(exc, requests.Timeout)
            else "TMDB API unreachable",
            status_problem=lambda status: "TMDB rate limit reached" if status == 429
            else f"TMDB server error (HTTP {status})",
            give_up=lambda problem, attempts: TMDBError(problem),
            max_retry_after=MAX_RETRY_AFTER, logger=log)

    # -- endpoints ----------------------------------------------------------------------------------
    def get_configuration(self) -> dict[str, Any]:
        """TMDB's ``/configuration`` object, requested once per client and then cached.

        Contract:
            Raises:
                TMDBError: Any error of ``request`` (nothing is cached then, so the next call
                    asks again). The object's content is not checked here.
        """
        if self._config is None:
            self._config = self.request("/configuration")
        return self._config

    def search_movie(self, query: str, language: str, include_adult: bool, year: Optional[int] = None,
                     region: str = "") -> list[dict[str, Any]]:
        data = self.request("/search/movie", {"query": query, "language": language, "year": year,
                                              "include_adult": str(bool(include_adult)).lower(), "region": region})
        return _search_results(data)

    def search_tv(self, query: str, language: str, include_adult: bool, year: Optional[int] = None) -> list[dict[str, Any]]:
        data = self.request("/search/tv", {"query": query, "language": language, "first_air_date_year": year,
                                           "include_adult": str(bool(include_adult)).lower()})
        return _search_results(data)

    def find_by_external_id(self, external_id: str, source: str, language: str) -> dict[str, Any]:
        """The ``/find`` answer for an external ID (``source`` like ``imdb_id``).

        Contract:
            Raises:
                TMDBInvalidId: ``external_id`` is not 1-32 ASCII letters/digits; nothing is requested.
                TMDBError: Any error of ``request``.
        """
        external_id = _checked(external_id, _EXTERNAL_ID, "external ID")
        return self.request(f"/find/{external_id}", {"external_source": source, "language": language})

    def get_movie(self, movie_id: str, language: str, image_languages: str, video_languages: str) -> dict[str, Any]:
        """Movie details with credits, external IDs, images, videos, keywords and release dates.

        ``get_tv``, ``get_season`` and ``get_episode`` follow the same contract.

        Contract:
            Raises:
                TMDBInvalidId: The ID is not 1-12 ASCII digits; nothing is requested.
                TMDBNotFound: TMDB answers 404 for the item.
                TMDBError: Any other error of ``request``.
                ValueError, TypeError: ``get_season``/``get_episode`` only, when a season or
                    episode number is not convertible by ``int()`` (implicit, not caught).
        """
        movie_id = _checked(movie_id, _TMDB_ID, "TMDB ID")
        return self.request(f"/movie/{movie_id}", {
            "language": language,
            "append_to_response": "credits,external_ids,images,videos,keywords,release_dates",
            "include_image_language": image_languages,
            "include_video_language": video_languages,
        })

    def get_tv(self, tv_id: str, language: str, image_languages: str, video_languages: str) -> dict[str, Any]:
        tv_id = _checked(tv_id, _TMDB_ID, "TMDB ID")
        return self.request(f"/tv/{tv_id}", {
            "language": language,
            "append_to_response": "aggregate_credits,external_ids,images,videos,keywords,content_ratings",
            "include_image_language": image_languages,
            "include_video_language": video_languages,
        })

    def get_season(self, tv_id: str, season: int, language: str, image_languages: str) -> dict[str, Any]:
        tv_id = _checked(tv_id, _TMDB_ID, "TMDB ID")
        return self.request(f"/tv/{tv_id}/season/{int(season)}", {
            "language": language, "append_to_response": "external_ids,images",
            "include_image_language": image_languages,
        })

    def get_episode(self, tv_id: str, season: int, episode: int, language: str, image_languages: str) -> dict[str, Any]:
        tv_id = _checked(tv_id, _TMDB_ID, "TMDB ID")
        return self.request(f"/tv/{tv_id}/season/{int(season)}/episode/{int(episode)}", {
            "language": language, "append_to_response": "credits,external_ids,images",
            "include_image_language": image_languages,
        })
