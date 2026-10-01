"""Minimal TMDB API v3 client (documented API only).

* Authentication: the API Read Access Token as a Bearer header when configured,
  otherwise the v3 API key as ``api_key`` query parameter.
* Connection/read timeouts, connection reuse, bounded concurrency.
* Bounded retries with backoff for timeouts, connection errors, 429 (honouring
  ``Retry-After``) and 5xx. 401/403/404 are never retried.
* Error messages never contain request URLs or credentials.
"""

from __future__ import annotations

import email.utils
import logging
import threading
import time
from typing import Any, Callable, Optional

import requests

from xtream_strm import APP_NAME, __version__

log = logging.getLogger(__name__)

API_BASE = "https://api.themoviedb.org/3"
MAX_RETRY_AFTER = 30.0


class TMDBError(Exception):
    """Transient or unexpected failure; the message is safe to show."""


class TMDBAuthError(TMDBError):
    pass


class TMDBNotFound(TMDBError):
    pass


class TMDBClient:
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
        self._config: Optional[dict] = None
        self.request_count = 0

    def close(self) -> None:
        self.session.close()

    # -- transport ---------------------------------------------------------------------------------
    def _auth(self, params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        headers = {"Accept": "application/json", "User-Agent": f"{APP_NAME}/{__version__}"}
        if self.read_token:
            headers["Authorization"] = f"Bearer {self.read_token}"
        else:
            params = {**params, "api_key": self.api_key}
        return params, headers

    def _retry_after(self, response) -> float:
        value = (getattr(response, "headers", None) or {}).get("Retry-After")
        if value:
            try:
                return min(MAX_RETRY_AFTER, max(0.0, float(value)))
            except ValueError:
                parsed = email.utils.parsedate_to_datetime(value) if value else None
                if parsed is not None:
                    return min(MAX_RETRY_AFTER, max(0.0, parsed.timestamp() - time.time()))
        return 0.0

    def request(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """GET ``path`` and return the decoded JSON object."""
        query, headers = self._auth({k: v for k, v in (params or {}).items() if v not in (None, "")})
        url = API_BASE + path
        attempt = 0
        while True:
            attempt += 1
            wait = self.backoff * (2 ** (attempt - 1))
            try:
                with self._gate:
                    self.request_count += 1
                    response = self.session.get(url, params=query, headers=headers, timeout=self.timeout)
            except requests.Timeout:
                problem = "TMDB API request timed out"
            except requests.RequestException:
                problem = "TMDB API unreachable"
            else:
                status = response.status_code
                if status == 200:
                    try:
                        data = response.json()
                    except ValueError:
                        raise TMDBError("TMDB returned a malformed response") from None
                    if not isinstance(data, dict):
                        raise TMDBError("TMDB returned an unexpected response")
                    return data
                if status == 401:
                    raise TMDBAuthError("Authentication failed")
                if status == 403:
                    raise TMDBAuthError("Access denied by TMDB (check the API credential)")
                if status == 404:
                    raise TMDBNotFound("Not found at TMDB")
                if status == 429:
                    problem = "TMDB rate limit reached"
                    wait = max(wait, self._retry_after(response))
                elif 500 <= status < 600:
                    problem = f"TMDB server error (HTTP {status})"
                else:
                    raise TMDBError(f"TMDB request failed (HTTP {status})")
            if attempt > self.retries:
                raise TMDBError(problem)
            log.debug("%s; retrying in %.1fs (attempt %d)", problem, wait, attempt)
            self.sleep(wait)

    # -- endpoints ----------------------------------------------------------------------------------
    def get_configuration(self) -> dict[str, Any]:
        if self._config is None:
            self._config = self.request("/configuration")
        return self._config

    def search_movie(self, query: str, language: str, include_adult: bool, year: Optional[int] = None,
                     region: str = "") -> list[dict]:
        data = self.request("/search/movie", {"query": query, "language": language, "year": year,
                                              "include_adult": str(bool(include_adult)).lower(), "region": region})
        return [r for r in data.get("results") or [] if isinstance(r, dict)]

    def search_tv(self, query: str, language: str, include_adult: bool, year: Optional[int] = None) -> list[dict]:
        data = self.request("/search/tv", {"query": query, "language": language, "first_air_date_year": year,
                                           "include_adult": str(bool(include_adult)).lower()})
        return [r for r in data.get("results") or [] if isinstance(r, dict)]

    def find_by_external_id(self, external_id: str, source: str, language: str) -> dict[str, Any]:
        return self.request(f"/find/{external_id}", {"external_source": source, "language": language})

    def get_movie(self, movie_id: str, language: str, image_languages: str, video_languages: str) -> dict[str, Any]:
        return self.request(f"/movie/{movie_id}", {
            "language": language,
            "append_to_response": "credits,external_ids,images,videos,keywords,release_dates",
            "include_image_language": image_languages,
            "include_video_language": video_languages,
        })

    def get_tv(self, tv_id: str, language: str, image_languages: str, video_languages: str) -> dict[str, Any]:
        return self.request(f"/tv/{tv_id}", {
            "language": language,
            "append_to_response": "aggregate_credits,external_ids,images,videos,keywords,content_ratings",
            "include_image_language": image_languages,
            "include_video_language": video_languages,
        })

    def get_season(self, tv_id: str, season: int, language: str, image_languages: str) -> dict[str, Any]:
        return self.request(f"/tv/{tv_id}/season/{int(season)}", {
            "language": language, "append_to_response": "external_ids,images",
            "include_image_language": image_languages,
        })

    def get_episode(self, tv_id: str, season: int, episode: int, language: str, image_languages: str) -> dict[str, Any]:
        return self.request(f"/tv/{tv_id}/season/{int(season)}/episode/{int(episode)}", {
            "language": language, "append_to_response": "credits,external_ids,images",
            "include_image_language": image_languages,
        })
