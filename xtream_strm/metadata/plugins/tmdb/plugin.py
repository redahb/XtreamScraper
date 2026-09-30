"""TMDB scraper plugin.

Responsibilities: TMDB API behaviour, external-ID lookup, search, conversion of search
results into shared :class:`~xtream_strm.core.matching.MatchCandidate` objects, and
mapping details to the normalized model. Fuzzy matching, merging, bindings and NFO
writing are done by the core.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from ....core.matching import MatchCandidate, TitleMatcher
from ...models import MetadataResult
from ...plugin import (
    Attribution,
    Capability,
    ConfigError,
    ConfigField,
    FieldType,
    ScraperPlugin,
    ScraperSession,
    validate_against_schema,
)
from ...results import FetchOutcome, MatchMethod, MatchOutcome, MatchQuery
from .client import TMDBAuthError, TMDBClient, TMDBError, TMDBNotFound
from .mapping import (
    ImageUrls,
    map_episode,
    map_movie,
    map_season,
    map_tv,
    year_of,
)

log = logging.getLogger(__name__)

_LANGUAGE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")
_REGION = re.compile(r"^[A-Z]{2}$")
# Our external-ID namespaces -> TMDB /find external_source values (only officially supported ones).
FIND_SOURCES = {"imdb": "imdb_id", "tvdb": "tvdb_id", "wikidata": "wikidata_id"}

ATTRIBUTION = "This product uses the TMDB API but is not endorsed or certified by TMDB."

ClientFactory = Callable[[dict[str, Any]], TMDBClient]


def _default_client(config: dict[str, Any]) -> TMDBClient:
    return TMDBClient(api_key=config.get("api_key") or "", read_token=config.get("read_token") or "")


class TMDBSession(ScraperSession):
    """One scrape job. Caches configuration, series and season details for the job."""

    def __init__(self, client: TMDBClient, config: dict[str, Any], matcher: TitleMatcher) -> None:
        self.client = client
        self.language = config.get("language") or "en-US"
        self.region = config.get("region") or ""
        self.include_adult = bool(config.get("include_adult"))
        self.threshold = float(config.get("threshold") if config.get("threshold") is not None else 85)
        self.matcher = matcher
        lang = self.language.split("-")[0]
        self.image_languages = f"{lang},null" if lang == "en" else f"{lang},null,en"
        self.video_languages = f"{lang},null" if lang == "en" else f"{lang},en,null"
        self._urls: Optional[ImageUrls] = None
        self._tv_cache: dict[str, dict] = {}
        self._season_cache: dict[tuple[str, int], dict] = {}

    def close(self) -> None:
        self.client.close()

    @property
    def urls(self) -> ImageUrls:
        if self._urls is None:
            self._urls = ImageUrls(self.client.get_configuration())
        return self._urls

    # -- helpers ---------------------------------------------------------------------------------
    @staticmethod
    def _fetch(call: Callable[[], MetadataResult]) -> FetchOutcome:
        try:
            return FetchOutcome.ok(call())
        except TMDBNotFound:
            return FetchOutcome.not_found("not found at TMDB")
        except TMDBError as exc:
            return FetchOutcome.error(str(exc))

    def _find(self, external_ids: dict[str, str], result_key: str) -> Optional[str]:
        """TMDB ID via /find for the first supported external ID that resolves."""
        for namespace, source in FIND_SOURCES.items():
            value = (external_ids.get(namespace) or "").strip()
            if not value:
                continue
            try:
                data = self.client.find_by_external_id(value, source, self.language)
            except TMDBNotFound:
                continue  # an unknown or malformed external ID is simply no result
            results = [r for r in data.get(result_key) or [] if isinstance(r, dict) and r.get("id") is not None]
            if results:
                return str(results[0]["id"])
        return None

    def _candidates(self, results: list[dict], title_key: str, original_key: str, date_key: str) -> list[MatchCandidate]:
        candidates = []
        for r in results:
            if r.get("id") is None or not (r.get(title_key) or r.get(original_key)):
                continue
            if r.get("adult") and not self.include_adult:
                continue
            title = str(r.get(title_key) or r.get(original_key))
            original = r.get(original_key)
            candidates.append(MatchCandidate(
                remote_id=str(r["id"]), title=title,
                alternate_titles=[str(original)] if original and original != title else [],
                year=year_of(r.get(date_key)),
                popularity=r.get("popularity") if isinstance(r.get("popularity"), (int, float)) else None,
            ))
        return candidates

    def _match(self, query: MatchQuery, find_key: str, search: Callable[[Optional[int]], list[dict]],
               keys: tuple[str, str, str]) -> MatchOutcome:
        try:
            found = self._find(query.external_ids, find_key)
            if found:
                return MatchOutcome.matched(found, MatchMethod.EXTERNAL_ID)
            method = MatchMethod.TITLE_YEAR if query.year else MatchMethod.TITLE_ONLY
            candidates = self._candidates(search(query.year), *keys) if query.year else []
            if candidates:
                decision = self.matcher.decide(query.title, query.year, candidates, self.threshold)
                if decision.matched:
                    return MatchOutcome.from_decision(decision, method)
            # The source year may be wrong: retry without the year filter and score everything.
            candidates += self._candidates(search(None), *keys)
            decision = self.matcher.decide(query.title, query.year, candidates, self.threshold)
            return MatchOutcome.from_decision(decision, method)
        except TMDBError as exc:
            return MatchOutcome.error(str(exc))

    # -- movies ---------------------------------------------------------------------------------------
    def match_movie(self, query: MatchQuery) -> MatchOutcome:
        return self._match(
            query, "movie_results",
            lambda year: self.client.search_movie(query.title, self.language, self.include_adult, year, self.region),
            ("title", "original_title", "release_date"),
        )

    def get_movie(self, remote_id: str) -> FetchOutcome:
        return self._fetch(lambda: map_movie(
            self.client.get_movie(remote_id, self.language, self.image_languages, self.video_languages),
            self.urls, self.language, self.region))

    # -- TV -------------------------------------------------------------------------------------------
    def match_series(self, query: MatchQuery) -> MatchOutcome:
        return self._match(
            query, "tv_results",
            lambda year: self.client.search_tv(query.title, self.language, self.include_adult, year),
            ("name", "original_name", "first_air_date"),
        )

    def _tv(self, tv_id: str) -> dict:
        if tv_id not in self._tv_cache:
            self._tv_cache[tv_id] = self.client.get_tv(tv_id, self.language, self.image_languages, self.video_languages)
        return self._tv_cache[tv_id]

    def get_series(self, remote_id: str) -> FetchOutcome:
        return self._fetch(lambda: map_tv(self._tv(remote_id), self.urls, self.language, self.region))

    def _season(self, tv_id: str, season: int) -> dict:
        key = (tv_id, int(season))
        if key not in self._season_cache:
            self._season_cache[key] = self.client.get_season(tv_id, season, self.language, self.image_languages)
        return self._season_cache[key]

    def get_season(self, series_remote_id: str, season_number: int) -> FetchOutcome:
        return self._fetch(lambda: map_season(self._season(series_remote_id, season_number), self.urls, self.language))

    def get_episode(self, series_remote_id: str, season_number: int, episode_number: int) -> FetchOutcome:
        return self._fetch(lambda: map_episode(
            self.client.get_episode(series_remote_id, season_number, episode_number, self.language,
                                    self.image_languages),
            self.urls, self.language))


class TMDBPlugin(ScraperPlugin):
    plugin_id = "tmdb"
    display_name = "TMDB"
    version = "1.0.0"
    capabilities = frozenset({
        Capability.MOVIES, Capability.SERIES, Capability.SEASONS, Capability.EPISODES,
        Capability.ARTWORK, Capability.EXTERNAL_IDS, Capability.RATINGS,
    })
    id_namespace = "tmdb"
    attribution = Attribution(ATTRIBUTION, "https://www.themoviedb.org/")

    def __init__(self, client_factory: Optional[ClientFactory] = None) -> None:
        self.client_factory = client_factory or _default_client

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField("api_key", "API Key (v3)", FieldType.SECRET,
                        help="From your TMDB account's API settings. Either this or the read access token is required."),
            ConfigField("read_token", "API Read Access Token (optional)", FieldType.SECRET,
                        help="Used instead of the API key when set."),
            ConfigField("threshold", "Title match threshold", FieldType.INTEGER, default=85, minimum=0, maximum=100,
                        help="Minimum confidence (0–100) for a title/year match."),
            ConfigField("language", "Metadata language", default="en-US",
                        help="ISO language code, optionally with country: en-US, nl-NL, de..."),
            ConfigField("region", "Region", default="",
                        help="Optional ISO country code (e.g. NL) for release dates and certifications."),
            ConfigField("include_adult", "Include adult titles", FieldType.BOOLEAN, default=False),
        ]

    def validate_config(self, values: dict[str, Any]) -> dict[str, Any]:
        clean = validate_against_schema(self.config_schema(), values)
        language = clean.get("language") or "en-US"
        parts = language.split("-")
        language = parts[0].lower() + (f"-{parts[1].upper()}" if len(parts) == 2 else "")
        if not _LANGUAGE.match(language):
            raise ConfigError("Metadata language must look like en-US or nl")
        region = (clean.get("region") or "").strip().upper()
        if region and not _REGION.match(region):
            raise ConfigError("Region must be a two-letter country code such as NL or US (or empty)")
        if clean.get("threshold") is None:
            clean["threshold"] = 85
        clean.update(language=language, region=region)
        return clean

    def is_configured(self, config: dict[str, Any]) -> bool:
        return bool(config.get("api_key") or config.get("read_token"))

    def test_connection(self, config: dict[str, Any]) -> tuple[bool, str]:
        if not self.is_configured(config):
            return False, "No TMDB API key or read access token configured"
        client = self.client_factory(config)
        try:
            data = client.get_configuration()
        except TMDBAuthError as exc:
            return False, str(exc)
        except TMDBError as exc:
            return False, "TMDB API unreachable" if "unreachable" in str(exc) or "timed out" in str(exc) else str(exc)
        finally:
            client.close()
        if not isinstance(data.get("images"), dict):
            return False, "TMDB returned an unexpected response"
        return True, "TMDB connection successful"

    def create_session(self, config: dict[str, Any]) -> ScraperSession:
        return TMDBSession(self.client_factory(config), config, TitleMatcher(self.scoring_profile))
