"""OMDb scraper plugin.

OMDb has no title IDs of its own: its identity is the IMDb ID, so remote IDs are IMDb
IDs, the external-ID namespace is ``imdb`` and no ``<uniqueid type="omdb">`` exists. The
binding stays plugin-specific (``plugin_id="omdb"``).

Matching (the core's order): stored binding -> IMDb ID already in the NFO (both fetched
directly with ``i=``) -> ``s=`` search with the type (and the year first, when known)
scored by the shared :class:`~xtreamscraper.core.matching.TitleMatcher`. The ``t=`` endpoint
is not used for matching because it returns OMDb's own pick, not candidates.

OMDb keys are request-limited, so a session caches every response for the job, never
requests the same resource twice, and stops calling OMDb for the rest of the job once
the request limit is reached or the key is rejected (``ScraperSession.suspended``).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from xtreamscraper.artwork.download import ArtworkDownloadError, validate_image
from xtreamscraper.core.matching import MatchCandidate, TitleMatcher, normalize_title
from xtreamscraper.core.matching.models import Decision
from xtreamscraper.utils.timeutil import now_iso
from xtreamscraper.metadata.models import MediaType
from xtreamscraper.metadata.plugin import (
    ArtworkPayload,
    Attribution,
    Capability,
    ConfigError,
    ConfigField,
    FieldType,
    ScraperPlugin,
    ScraperSession,
    TestAction,
    TestResult,
    validate_against_schema,
)
from xtreamscraper.metadata.results import FetchOutcome, MatchMethod, MatchOutcome, MatchQuery
from .client import (
    API_BASE,
    POSTER_BASE,
    OMDbAuthError,
    OMDbClient,
    OMDbError,
    OMDbMalformed,
    OMDbNotFound,
    OMDbQuotaError,
    PosterNotFound,
    PosterUnavailable,
)
from .mapping import (
    SeasonListing,
    imdb_id,
    map_details,
    media_type_of,
    parse_int,
    parse_season_listing,
    parse_year,
    positive_int,
)

log = logging.getLogger(__name__)

ATTRIBUTION = ("Metadata provided by the OMDb API (The Open Movie Database), licensed under CC BY-NC 4.0 "
               "(https://creativecommons.org/licenses/by-nc/4.0/); for personal, non-commercial use.")
QUOTA_MESSAGE = "OMDb request limit reached; OMDb was skipped for the remainder of this job."
AUTH_MESSAGE = "OMDb API key invalid; OMDb was skipped for the remainder of this job."
MAX_SEARCH_PAGES = 3
PAGE_SIZE = 10
MAX_POSTER_HEIGHT = 3000
TEST_TITLE = "tt0111161"  # a well-known title for connection tests

# Status entries shown on the settings page
MAIN_API, POSTER_API, LAST_SUCCESS, LAST_JOB = ("Main API", "Poster API", "Last successful request",
                                                "OMDb requests in the last job")

ClientFactory = Callable[[dict[str, Any]], OMDbClient]


def _default_client(config: dict[str, Any]) -> OMDbClient:
    return OMDbClient(api_key=config.get("api_key") or "", base_url=config.get("api_base_url") or API_BASE,
                      poster_base_url=config.get("poster_base_url") or POSTER_BASE)


class OMDbSuspended(OMDbError):
    """OMDb is not called again during this job."""


class OMDbSession(ScraperSession):
    def __init__(self, client: OMDbClient, config: dict[str, Any], matcher: TitleMatcher) -> None:
        self.client = client
        self.matcher = matcher
        self.threshold = float(config.get("threshold") if config.get("threshold") is not None else 85)
        self.use_poster_api = bool(config.get("use_poster_api"))
        self.poster_height = min(MAX_POSTER_HEIGHT, max(1, int(config.get("poster_height") or 1080)))
        self.suspended = None
        # Per-job caches: one request per exact resource (not-found answers are cached too).
        self._search_cache: dict[tuple, Any] = {}
        self._detail_cache: dict[str, Any] = {}
        self._episode_cache: dict[tuple, Any] = {}
        self._season_cache: dict[tuple, Any] = {}
        self.main_api: Optional[str] = None
        self.poster_api: Optional[str] = None  # "available" / "unavailable for this key" (this job)
        self.last_success: Optional[str] = None

    def close(self) -> None:
        self.client.close()

    # -- transport with caching and job-wide suspension ------------------------------------------------
    def _suspend(self, message: str, main_status: str) -> None:
        if not self.suspended:
            log.warning("%s", message)
        self.suspended = message
        self.main_api = main_status

    def _cached(self, cache: dict, key: Any, call: Callable[[], Any]) -> Any:
        if key in cache:
            value = cache[key]
            if isinstance(value, OMDbNotFound):
                raise value
            return value
        if self.suspended:
            raise OMDbSuspended(self.suspended)
        try:
            value = call()
        except OMDbNotFound as exc:
            cache[key] = exc  # a definitive answer: authentication and quota are fine
            self._success()
            raise
        except OMDbQuotaError:
            self._suspend(QUOTA_MESSAGE, "Request quota exhausted")
            raise
        except OMDbAuthError:
            self._suspend(AUTH_MESSAGE, "API key invalid")
            raise
        cache[key] = value
        self._success()
        return value

    def _success(self) -> None:
        self.main_api = "available"
        self.last_success = now_iso()

    def _details(self, remote_id: str) -> dict:
        return self._cached(self._detail_cache, remote_id, lambda: self.client.get_by_id(remote_id))

    def _search(self, title: str, media_type: str, year: Optional[int], page: int) -> dict:
        key = (normalize_title(title), year, media_type, page)
        return self._cached(self._search_cache, key, lambda: self.client.search(title, media_type, year, page))

    # -- matching ---------------------------------------------------------------------------------------
    @staticmethod
    def _candidates(results: list, media_type: str) -> list[MatchCandidate]:
        candidates = []
        for r in results:
            if not isinstance(r, dict) or media_type_of(r) != media_type:
                continue  # only the requested type is a candidate
            remote, title = imdb_id(r.get("imdbID")), r.get("Title")
            if not remote or not isinstance(title, str) or not title.strip() or title.strip().casefold() == "n/a":
                continue
            candidates.append(MatchCandidate(remote_id=remote, title=title.strip(), year=parse_year(r.get("Year"))))
        return candidates

    def _match(self, query: MatchQuery, media_type: str) -> MatchOutcome:
        method = MatchMethod.TITLE_YEAR if query.year else MatchMethod.TITLE_ONLY
        candidates: list[MatchCandidate] = []
        decision = None
        try:
            # With a known year first (remakes share titles), then once without it. Pages are only
            # fetched while nothing acceptable was found, and never more than MAX_SEARCH_PAGES.
            for year in ([query.year, None] if query.year else [None]):
                for page in range(1, MAX_SEARCH_PAGES + 1):
                    data = self._search(query.title, media_type, year, page)
                    results = data.get("Search") if isinstance(data.get("Search"), list) else []
                    candidates += self._candidates(results, media_type)
                    if candidates:
                        decision = self.matcher.decide(query.title, query.year, candidates, self.threshold)
                        if decision.decision is not Decision.UNMATCHED:
                            return MatchOutcome.from_decision(decision, method)
                    total = parse_int(data.get("totalResults")) or 0
                    if not results or page * PAGE_SIZE >= total:
                        break
        except OMDbError as exc:
            return MatchOutcome.error(str(exc))
        if decision is None:
            return MatchOutcome.unmatched(message="no OMDb search results")
        return MatchOutcome.from_decision(decision, method)

    def _fetch(self, remote_id: str, media_type: str, media: MediaType) -> FetchOutcome:
        requested = imdb_id(remote_id)
        if not requested:
            return FetchOutcome.not_found(f"{remote_id!r} is not an IMDb ID")
        try:
            data = self._details(requested)
        except OMDbNotFound:
            return FetchOutcome.not_found("not found at OMDb")
        except OMDbError as exc:
            return FetchOutcome.error(str(exc))
        kind = media_type_of(data)
        if kind and kind != media_type:
            return FetchOutcome.not_found(f"{requested} is a {kind} at OMDb, not a {media_type}")
        returned = imdb_id(data.get("imdbID"))
        if returned and returned != requested:
            return FetchOutcome.not_found(f"OMDb returned {returned} for {requested}")
        meta = map_details(data, media, poster_ref=requested if self.use_poster_api else None)
        meta.external_ids["imdb"] = requested
        meta.default_id_type, meta.remote_id = "imdb", requested
        return FetchOutcome.ok(meta, requested)

    def match_movie(self, query: MatchQuery) -> MatchOutcome:
        return self._match(query, "movie")

    def get_movie(self, remote_id: str) -> FetchOutcome:
        return self._fetch(remote_id, "movie", MediaType.MOVIE)

    def match_series(self, query: MatchQuery) -> MatchOutcome:
        return self._match(query, "series")

    def get_series(self, remote_id: str) -> FetchOutcome:
        return self._fetch(remote_id, "series", MediaType.SERIES)

    # get_season: OMDb has no standalone season metadata -> the default "unsupported".

    def get_episode(self, series_remote_id: str, season_number: int, episode_number: int) -> FetchOutcome:
        """Series IMDb ID + local season/episode numbers (authoritative); never a title search."""
        series = imdb_id(series_remote_id)
        if not series:
            return FetchOutcome.not_found(f"{series_remote_id!r} is not an IMDb ID")
        key = (series, int(season_number), int(episode_number))
        try:
            data = self._cached(self._episode_cache, key,
                                lambda: self.client.get_episode(series, season_number, episode_number))
        except OMDbNotFound:
            return FetchOutcome.not_found("episode not found at OMDb")
        except OMDbError as exc:
            return FetchOutcome.error(str(exc))
        kind = media_type_of(data)
        if kind and kind != "episode":
            return FetchOutcome.not_found(f"OMDb returned a {kind}, not an episode")
        season, episode, parent = positive_int(data.get("Season")), positive_int(data.get("Episode")), \
            imdb_id(data.get("seriesID"))
        if (season is not None and season != int(season_number)) or \
                (episode is not None and episode != int(episode_number)) or (parent and parent != series):
            return FetchOutcome.not_found(f"OMDb returned a different episode for S{int(season_number):02d}"
                                          f"E{int(episode_number):02d}")
        remote = imdb_id(data.get("imdbID"))
        if not remote or remote == series:
            return FetchOutcome.not_found("OMDb returned no IMDb ID for the episode")
        meta = map_details(data, MediaType.EPISODE)
        meta.season_number, meta.episode_number = int(season_number), int(episode_number)
        return FetchOutcome.ok(meta, remote)

    def season_listing(self, series_remote_id: str, season_number: int) -> Optional[SeasonListing]:
        """The episodes OMDb knows for a season (informational; possibly incomplete)."""
        series = imdb_id(series_remote_id)
        if not series:
            return None
        try:
            data = self._cached(self._season_cache, (series, int(season_number)),
                                lambda: self.client.get_season(series, season_number))
        except OMDbError:
            return None
        return parse_season_listing(data)

    # -- high-resolution posters -----------------------------------------------------------------------
    def fetch_artwork(self, download_ref: str) -> Optional[ArtworkPayload]:
        """Poster API image for a local download. The key-bearing URL never leaves the client."""
        ref = imdb_id(download_ref)
        if not ref or not self.use_poster_api or self.suspended or self.poster_api == "unavailable for this key":
            return None
        try:
            image = self.client.get_poster(ref, self.poster_height)
        except (PosterUnavailable, OMDbAuthError):
            # Entitlement failure: remembered for this job, the public posters are used instead.
            self.poster_api = "unavailable for this key"
            log.info("OMDb Poster API unavailable for this key; using public posters for the rest of this job")
            return None
        except OMDbQuotaError:
            self._suspend(QUOTA_MESSAGE, "Request quota exhausted")
            return None
        except PosterNotFound:
            return None
        except OMDbError as exc:
            log.info("OMDb Poster API request failed for %s: %s", ref, exc)
            return None
        self.poster_api = "available"
        return ArtworkPayload(image.data, image.content_type)

    def status_update(self) -> dict[str, Any]:
        status: dict[str, Any] = {LAST_JOB: str(self.client.request_count)}
        if self.main_api:
            status[MAIN_API] = self.main_api
        if self.last_success:
            status[LAST_SUCCESS] = self.last_success
        if self.poster_api:
            status[POSTER_API] = self.poster_api
        return status


class OMDbPlugin(ScraperPlugin):
    plugin_id = "omdb"
    display_name = "OMDb"
    version = "20261002"
    capabilities = frozenset({
        Capability.MOVIES, Capability.SERIES, Capability.EPISODES,
        Capability.ARTWORK, Capability.RATINGS, Capability.EXTERNAL_IDS,
    })
    id_namespace = "imdb"  # OMDb's remote IDs are IMDb IDs; there is no "omdb" ID
    attribution = Attribution(ATTRIBUTION, "https://www.omdbapi.com/")

    def __init__(self, client_factory: Optional[ClientFactory] = None) -> None:
        self.client_factory = client_factory or _default_client

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField("api_key", "API Key", FieldType.SECRET, required=True,
                        help="Your OMDb API key (https://www.omdbapi.com/apikey.aspx)."),
            ConfigField("threshold", "Match threshold", FieldType.INTEGER, default=85, minimum=0, maximum=100,
                        help="Minimum confidence (0–100) for a title/year match."),
            ConfigField("use_poster_api", "Use high-resolution Poster API", FieldType.BOOLEAN, default=False,
                        help="Patron keys only. Used for local poster downloads; the public poster is the fallback "
                             "and the only poster ever written to NFO files."),
            ConfigField("poster_height", "Poster height", FieldType.INTEGER, default=1080, minimum=1,
                        maximum=MAX_POSTER_HEIGHT, help="Requested height in pixels (at most 3000)."),
            ConfigField("api_base_url", "API base URL", default=API_BASE, advanced=True,
                        help="Change only if OMDb gave you a private server URL."),
            ConfigField("poster_base_url", "Poster API base URL", default=POSTER_BASE, advanced=True,
                        help="Change only if OMDb gave you a private Poster API URL."),
        ]

    def validate_config(self, values: dict[str, Any]) -> dict[str, Any]:
        clean = validate_against_schema(self.config_schema(), values)
        if clean.get("threshold") is None:
            clean["threshold"] = 85
        if clean.get("poster_height") is None:
            clean["poster_height"] = 1080
        for key, default, label in (("api_base_url", API_BASE, "API base URL"),
                                    ("poster_base_url", POSTER_BASE, "Poster API base URL")):
            url = (clean.get(key) or "").strip() or default
            if not url.lower().startswith("https://") or any(c.isspace() for c in url) or "?" in url or "#" in url:
                raise ConfigError(f"{label} must be an https:// URL without a query string")
            clean[key] = url
        return clean

    def is_configured(self, config: dict[str, Any]) -> bool:
        return bool((config.get("api_key") or "").strip())

    def _client(self, config: dict[str, Any]) -> Optional[OMDbClient]:
        try:
            return self.client_factory(config)
        except OMDbAuthError:
            return None

    def test_connection(self, config: dict[str, Any]) -> TestResult:
        """A lightweight authenticated lookup of one well-known title."""
        client = self._client(config) if self.is_configured(config) else None
        if client is None:
            return False, "OMDb API key missing", {MAIN_API: "API key missing"}
        try:
            client.request({"i": TEST_TITLE})
        except OMDbNotFound:
            pass  # a valid, authenticated answer
        except OMDbAuthError:
            return False, "OMDb API key invalid", {MAIN_API: "API key invalid"}
        except OMDbQuotaError:
            return False, "OMDb request limit reached", {MAIN_API: "Request quota exhausted"}
        except OMDbMalformed as exc:
            return False, str(exc), {MAIN_API: "malformed response"}
        except OMDbError as exc:
            text = str(exc)
            message = "OMDb API unreachable" if any(w in text for w in ("unreachable", "timed out", "server error")) \
                else text
            return False, message, {MAIN_API: "unreachable" if message == "OMDb API unreachable" else "error"}
        finally:
            client.close()
        return True, "OMDb connection successful", {MAIN_API: "available", LAST_SUCCESS: now_iso()}

    def extra_tests(self) -> list[TestAction]:
        return [TestAction("poster", "Test Poster API")]

    def run_test(self, key: str, config: dict[str, Any]) -> TestResult:
        if key != "poster":
            raise KeyError(key)
        client = self._client(config) if self.is_configured(config) else None
        if client is None:
            return False, "OMDb API key missing"
        height = min(MAX_POSTER_HEIGHT, max(1, int(config.get("poster_height") or 1080)))
        try:
            image = client.get_poster(TEST_TITLE, height)
            validate_image(image.data, image.content_type)
        except PosterUnavailable:
            return False, "Poster API unavailable for this key", {POSTER_API: "unavailable for this key"}
        except OMDbAuthError:
            # The Poster API rejects keys without access the same way as unknown keys: the
            # metadata API tells the two apart.
            try:
                client.request({"i": TEST_TITLE})
            except OMDbNotFound:
                pass
            except OMDbError:
                return False, "Invalid API key", {POSTER_API: "API key invalid"}
            return False, "Poster API unavailable for this key", {POSTER_API: "unavailable for this key"}
        except OMDbQuotaError:
            return False, "OMDb request limit reached", {MAIN_API: "Request quota exhausted"}
        except (OMDbError, ArtworkDownloadError):
            return False, "Poster request failed", {POSTER_API: "request failed"}
        finally:
            client.close()
        return True, "Poster API available", {POSTER_API: "available"}

    def create_session(self, config: dict[str, Any]) -> ScraperSession:
        return OMDbSession(self.client_factory(config), config, TitleMatcher(self.scoring_profile))
