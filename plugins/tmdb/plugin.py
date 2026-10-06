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
    TestResult,
    validate_against_schema,
)
from xtreamscraper.metadata.results import FetchOutcome, MatchMethod, MatchOutcome, MatchQuery, ResolveOutcome
from .client import TMDBAuthError, TMDBClient, TMDBError, TMDBFormatError, TMDBInvalidId, TMDBNotFound
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
SUSPENDED = "{}; TMDB was skipped for the remainder of this job."

ClientFactory = Callable[[dict[str, Any]], TMDBClient]


def _default_client(config: dict[str, Any]) -> TMDBClient:
    return TMDBClient(api_key=config.get("api_key") or "", read_token=config.get("read_token") or "")


class TMDBSession(ScraperSession):
    """One scrape job. Caches configuration, series and season details for the job.

    Contract:
        Preconditions:
            - ``config`` is a config cleaned by ``TMDBPlugin.validate_config``. A ``threshold``
              that ``float()`` cannot read raises ``ValueError``/``TypeError`` here (implicit).
            - One thread per session: the caches and the lazy ``urls`` are check-then-set
              without a lock (concurrent use only costs duplicate requests).
            - A cached series/season answer is reused for the whole job, even after TMDB changes it.
    """

    client: TMDBClient
    language: str
    region: str
    include_adult: bool
    threshold: float
    matcher: TitleMatcher
    image_languages: str
    video_languages: str

    def __init__(self, client: TMDBClient, config: dict[str, Any], matcher: TitleMatcher) -> None:
        self.client = client
        self.language = config.get("language") or "en-US"
        self.region = config.get("region") or ""
        self.include_adult = bool(config.get("include_adult"))
        threshold = config.get("threshold")
        self.threshold = float(threshold if threshold is not None else 85)
        self.matcher = matcher
        lang = self.language.split("-")[0]
        self.image_languages = f"{lang},null" if lang == "en" else f"{lang},null,en"
        self.video_languages = f"{lang},null" if lang == "en" else f"{lang},en,null"
        self._urls: Optional[ImageUrls] = None
        self._tv_cache: dict[str, dict[str, Any]] = {}
        self._season_cache: dict[tuple[str, int], dict[str, Any]] = {}

    def close(self) -> None:
        self.client.close()

    @property
    def urls(self) -> ImageUrls:
        """Image URL builder from ``/configuration``, built once per session.

        Contract:
            Raises:
                TMDBError: Any error of ``/configuration`` (nothing is cached, the next access
                    asks again).

            Silences:
                - A configuration without an image base URL gives an ``ImageUrls`` whose URLs are
                  all ``None``: items then map without artwork, with no error.
        """
        if self._urls is None:
            self._urls = ImageUrls(self.client.get_configuration())
        return self._urls

    # -- helpers ---------------------------------------------------------------------------------
    def _rejected(self, exc: TMDBAuthError) -> str:
        """A rejected credential fails every request: TMDB is skipped for the rest of the job."""
        if not self.suspended:
            log.warning("TMDB rejected the API credential (%s); skipped for the rest of this job", exc)
        self.suspended = SUSPENDED.format(exc)
        return str(exc)

    def _fetch(self, request: Callable[[], dict[str, Any]],
               mapper: Callable[[dict[str, Any], ImageUrls], MetadataResult]) -> FetchOutcome:
        """Request one item's details, then map them with the image configuration.

        Only the item request can give ``not_found``: a failing ``/configuration`` (even a 404)
        is an API error, which keeps the item's binding.

        Contract:
            Raises:
                Exception: Anything ``request`` or ``mapper`` raises that is not a ``TMDBError``
                    (a mapping bug, a non-numeric season number) propagates; the engine treats it
                    as an API error.

            Silences:
                - Every ``TMDBError``, as an outcome: ``TMDBInvalidId``/``TMDBNotFound`` from the
                  item request -> ``not_found``; ``TMDBAuthError`` -> ``error`` and the session is
                  suspended; ``TMDBFormatError`` -> ``provider_format_error``; others -> ``error``.
        """
        try:
            details = request()
        except TMDBInvalidId as exc:
            return FetchOutcome.not_found(str(exc))
        except TMDBNotFound:
            return FetchOutcome.not_found("not found at TMDB")
        except TMDBError as exc:
            return self._failed(exc)
        try:
            urls = self.urls
        except TMDBError as exc:
            return self._failed(exc, "TMDB configuration unavailable: ")
        return FetchOutcome.ok(mapper(details, urls))

    def _failed(self, exc: TMDBError, prefix: str = "") -> FetchOutcome:
        """The outcome of a failed request that does not prove the item is gone."""
        if isinstance(exc, TMDBAuthError):
            return FetchOutcome.error(self._rejected(exc))
        if isinstance(exc, TMDBFormatError):
            return FetchOutcome.provider_format_error(prefix + str(exc))
        return FetchOutcome.error(prefix + str(exc))

    def _find_one(self, namespace: str, value: str) -> dict[str, Any]:
        """The /find answer for one external ID (``TMDBNotFound`` for an unknown or malformed one)."""
        return self.client.find_by_external_id(value, FIND_SOURCES[namespace], self.language)

    @staticmethod
    def _found(data: dict[str, Any], result_key: str) -> Optional[str]:
        results = [r for r in data.get(result_key) or [] if isinstance(r, dict) and r.get("id") is not None]
        return str(results[0]["id"]) if results else None

    def _find(self, external_ids: dict[str, str], result_key: str) -> Optional[str]:
        """TMDB ID via /find for the first supported external ID that resolves.

        Namespaces are tried in ``FIND_SOURCES`` order (IMDb, TVDB, Wikidata), so an IMDb hit wins.

        Contract:
            Raises:
                TMDBError: Any /find failure other than not-found (auth, format, transport) stops
                    the lookup and propagates; later namespaces are not tried.

            Silences:
                - ``TMDBNotFound``/``TMDBInvalidId`` for one namespace (an unknown or malformed ID):
                  the next namespace is tried. ``None`` means "no external ID resolved".
                - A /find answer whose ``result_key`` entry is missing, empty or holds no objects
                  counts as no result (not as a format error). A number there raises ``TypeError``
                  from ``_found`` (implicit, not caught).
        """
        for namespace in FIND_SOURCES:
            value = (external_ids.get(namespace) or "").strip()
            if not value:
                continue
            try:
                data = self._find_one(namespace, value)
            except TMDBNotFound:
                continue  # an unknown or malformed external ID is simply no result
            found = self._found(data, result_key)
            if found:
                return found
        return None

    def resolve_manual_id(self, media_type: str, namespace: str, value: str) -> ResolveOutcome:
        """A manually entered IMDb/TVDB/Wikidata ID -> TMDB ID, through /find only (no title search).

        Contract:
            Preconditions:
                - ``value`` was normalized by ``TMDBPlugin.normalize_manual_id``.

            Silences:
                - Every ``TMDBError``, as an outcome: not-found or an invalid ID -> ``not_found``;
                  auth -> ``error`` and the session is suspended; format and transport -> ``error``
                  (a format change is not flagged as such here).
                - An answer that only has a result of the other media type -> ``not_found`` with
                  a message saying so.
        """
        if namespace not in FIND_SOURCES or media_type not in FIND_KEYS:
            return ResolveOutcome.unsupported()
        try:
            data = self._find_one(namespace, value)
        except TMDBNotFound:
            return ResolveOutcome.not_found(f"TMDB does not know {value}")
        except TMDBAuthError as exc:
            return ResolveOutcome.error(self._rejected(exc))
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

    def _candidates(self, results: list[dict[str, Any]], title_key: str, original_key: str,
                    date_key: str) -> list[MatchCandidate]:
        """Search results as match candidates.

        Contract:
            Silences:
                - Results without an ``id`` or without any title are skipped; adult results are
                  skipped unless ``include_adult`` is on. A non-numeric popularity is ``None`` and
                  an unreadable date gives no year.
        """
        candidates: list[MatchCandidate] = []
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

    def _match(self, query: MatchQuery, find_key: str, search: Callable[[Optional[int]], list[dict[str, Any]]],
               keys: tuple[str, str, str]) -> MatchOutcome:
        """External-ID lookup, then a year-filtered search, then an unfiltered one (shared matcher).

        The unfiltered pass scores the year-filtered candidates again together with the new ones;
        ``TitleMatcher`` drops duplicate remote IDs. It also runs when the first pass was ambiguous.

        Contract:
            Raises:
                Exception: Anything that is not a ``TMDBError`` (e.g. from the matcher) propagates.

            Silences:
                - Every ``TMDBError``, as an outcome: auth -> ``error`` and the session is
                  suspended; format -> ``provider_format_error``; others -> ``error``. A failing
                  /find is not skipped: it ends the match with that outcome.
        """
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
        except TMDBAuthError as exc:
            return MatchOutcome.error(self._rejected(exc))
        except TMDBFormatError as exc:
            return MatchOutcome.provider_format_error(str(exc))
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
        return self._fetch(
            lambda: self.client.get_movie(remote_id, self.language, self.image_languages, self.video_languages),
            lambda details, urls: map_movie(details, urls, self.language, self.region))

    # -- TV -------------------------------------------------------------------------------------------
    def match_series(self, query: MatchQuery) -> MatchOutcome:
        return self._match(
            query, "tv_results",
            lambda year: self.client.search_tv(query.title, self.language, self.include_adult, year),
            ("name", "original_name", "first_air_date"),
        )

    def _tv(self, tv_id: str) -> dict[str, Any]:
        if tv_id not in self._tv_cache:
            self._tv_cache[tv_id] = self.client.get_tv(tv_id, self.language, self.image_languages, self.video_languages)
        return self._tv_cache[tv_id]

    def get_series(self, remote_id: str) -> FetchOutcome:
        return self._fetch(lambda: self._tv(remote_id),
                           lambda details, urls: map_tv(details, urls, self.language, self.region))

    def _season(self, tv_id: str, season: int) -> dict[str, Any]:
        key = (tv_id, int(season))
        if key not in self._season_cache:
            self._season_cache[key] = self.client.get_season(tv_id, season, self.language, self.image_languages)
        return self._season_cache[key]

    def get_season(self, series_remote_id: str, season_number: int) -> FetchOutcome:
        return self._fetch(lambda: self._season(series_remote_id, season_number),
                           lambda details, urls: map_season(details, urls, self.language))

    def get_episode(self, series_remote_id: str, season_number: int, episode_number: int) -> FetchOutcome:
        return self._fetch(
            lambda: self.client.get_episode(series_remote_id, season_number, episode_number, self.language,
                                            self.image_languages),
            lambda details, urls: map_episode(details, urls, self.language))


class TMDBPlugin(ScraperPlugin):
    plugin_id = "tmdb"
    display_name = "TMDB"
    version = "20261004"
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
        """The canonical form of a typed ID or link: digits without leading zeros, ``tt...``
        lower case, ``Q...`` upper case.

        Contract:
            Raises:
                ManualIdError: The text is not a valid ID (or link) for ``namespace``, a TMDB/TVDB
                    ID is 0, or ``namespace`` is not one TMDB accepts (explicit).

            Silences:
                - Non-ASCII digits are accepted for TMDB/TVDB IDs and converted by ``int()``.
        """
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
        """Schema check, then the language normalized to ``xx`` / ``xx-YY`` and the region upper case.

        Contract:
            Raises:
                ConfigError: From the schema (e.g. a threshold outside 0-100), a language that is
                    not ``xx`` or ``xx-YY`` after normalizing, or a region that is not two letters.

            Silences:
                - A language with more than two parts keeps only the first (``zh-Hant-TW`` is
                  stored as ``zh``) without an error.
                - An empty language becomes ``en-US``; an empty threshold becomes 85.
        """
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

    def test_connection(self, config: dict[str, Any]) -> TestResult:
        """Request ``/configuration`` once with ``config`` and report ``(ok, message)``.

        Contract:
            Raises:
                Exception: Whatever ``client_factory`` raises (the default factory cannot raise
                    once ``is_configured`` is true).

            Silences:
                - Every ``TMDBError``, as ``(False, message)``. A message containing
                  "unreachable" or "timed out" is shown as "TMDB API unreachable" (substring
                  match on the client's text).
        """
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
