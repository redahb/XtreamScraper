"""TMDB scraper plugin.

Responsibilities: TMDB API behaviour, external-ID lookup, search, conversion of search
results into shared :class:`~xtreamscraper.core.matching.MatchCandidate` objects, and
mapping details to the normalized model. Fuzzy matching, merging, bindings and NFO
writing are done by the core.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from xtreamscraper.core.matching import MatchCandidate, TitleMatcher
from xtreamscraper.metadata.models import MetadataResult
from xtreamscraper.metadata.plugin import (
    Attribution,
    Capability,
    ConfigError,
    ConfigField,
    FieldType,
    ManualIdError,
    ManualIdField,
    ScraperPlugin,
    ScraperSession,
    validate_against_schema,
)
from xtreamscraper.metadata.results import FetchOutcome, MatchMethod, MatchOutcome, MatchQuery, ResolveOutcome
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
FIND_KEYS = {"movie": "movie_results", "series": "tv_results"}

# Manual IDs: a bare ID, or a link that contains one.
_TMDB_ID = re.compile(r"^\d{1,10}$")
_TMDB_URL = re.compile(r"themoviedb\.org/(?:movie|tv)/(\d{1,10})(?:\D|$)", re.IGNORECASE)
_IMDB_ID = re.compile(r"^tt\d{5,}$", re.IGNORECASE)
_IMDB_URL = re.compile(r"imdb\.com/title/(tt\d{5,})(?:\D|$)", re.IGNORECASE)
_TVDB_ID = re.compile(r"^\d{1,10}$")
_WIKIDATA_ID = re.compile(r"^Q\d{1,12}$", re.IGNORECASE)
_WIKIDATA_URL = re.compile(r"wikidata\.org/(?:wiki|entity)/(Q\d{1,12})(?:\D|$)", re.IGNORECASE)

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

    def _find_one(self, namespace: str, value: str) -> dict:
        """The /find answer for one external ID (``TMDBNotFound`` for an unknown or malformed one)."""
        return self.client.find_by_external_id(value, FIND_SOURCES[namespace], self.language)

    @staticmethod
    def _found(data: dict, result_key: str) -> Optional[str]:
        results = [r for r in data.get(result_key) or [] if isinstance(r, dict) and r.get("id") is not None]
        return str(results[0]["id"]) if results else None

    def _find(self, external_ids: dict[str, str], result_key: str) -> Optional[str]:
        """TMDB ID via /find for the first supported external ID that resolves."""
        for namespace in FIND_SOURCES:
            value = (external_ids.get(namespace) or "").strip()
            if not value:
                continue
            try:
                found = self._found(self._find_one(namespace, value), result_key)
            except TMDBNotFound:
                continue  # an unknown or malformed external ID is simply no result
            if found:
                return found
        return None

    def resolve_manual_id(self, media_type: str, namespace: str, value: str) -> ResolveOutcome:
        """A manually entered IMDb/TVDB/Wikidata ID -> TMDB ID, through /find only (no title search)."""
        if namespace not in FIND_SOURCES or media_type not in FIND_KEYS:
            return ResolveOutcome.unsupported()
        try:
            data = self._find_one(namespace, value)
        except TMDBNotFound:
            return ResolveOutcome.not_found(f"TMDB does not know {value}")
        except TMDBError as exc:
            return ResolveOutcome.error(str(exc))
        found = self._found(data, FIND_KEYS[media_type])
        if found:
            return ResolveOutcome.ok(found)
        other = "series" if media_type == "movie" else "movie"
        if self._found(data, FIND_KEYS[other]):
            return ResolveOutcome.not_found(f"{value} is a {'TV series' if other == 'series' else 'movie'} "
                                            f"at TMDB, not a {'movie' if media_type == 'movie' else 'TV series'}")
        return ResolveOutcome.not_found(f"TMDB has no {'movie' if media_type == 'movie' else 'TV series'} for {value}")

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
    version = "20261002"
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

    def manual_id_fields(self, media_type: str) -> list[ManualIdField]:
        if media_type not in FIND_KEYS:
            return []
        page = "movie" if media_type == "movie" else "tv"
        return [
            ManualIdField("tmdb", "TMDB ID", help=f"The number in the TMDB address, e.g. themoviedb.org/{page}/603. "
                          "A TMDB link works too.", placeholder="603", pattern_hint="digits"),
            ManualIdField("imdb", "IMDb ID", help="Looked up at TMDB. An IMDb link works too.",
                          placeholder="tt0133093", pattern_hint="tt + digits"),
            ManualIdField("tvdb", "TVDB ID", help="Looked up at TMDB.", placeholder="81189", pattern_hint="digits"),
            ManualIdField("wikidata", "Wikidata ID", help="Looked up at TMDB. A Wikidata link works too.",
                          placeholder="Q83495", pattern_hint="Q + digits"),
        ]

    def normalize_manual_id(self, namespace: str, value: str) -> str:
        text = (value or "").strip()
        if namespace == "tmdb":
            found = text if _TMDB_ID.match(text) else _group(_TMDB_URL, text)
            if found and int(found) > 0:
                return str(int(found))
            raise ManualIdError("A TMDB ID is a positive number, such as 603")
        if namespace == "imdb":
            found = text if _IMDB_ID.match(text) else _group(_IMDB_URL, text)
            if found:
                return found.lower()
            raise ManualIdError("An IMDb ID looks like tt0133093")
        if namespace == "tvdb":
            if _TVDB_ID.match(text) and int(text) > 0:
                return str(int(text))
            raise ManualIdError("A TVDB ID is a positive number, such as 81189")
        if namespace == "wikidata":
            found = text if _WIKIDATA_ID.match(text) else _group(_WIKIDATA_URL, text)
            if found:
                return found.upper()
            raise ManualIdError("A Wikidata ID looks like Q83495")
        raise ManualIdError("TMDB does not accept this ID type")

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


def _group(pattern: "re.Pattern[str]", text: str) -> Optional[str]:
    match = pattern.search(text)
    return match.group(1) if match else None
