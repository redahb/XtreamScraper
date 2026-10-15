"""Canal+ scraper plugin (the Canal+ websites of the Netherlands, Austria, Czechia, Slovakia and Hungary).

Responsibilities: Canal+ request behaviour (public search settings, the Algolia search index),
candidate retrieval, the media kind of each record (from its page address), series families
(season and episode records) and mapping to the normalized model. Scoring, thresholds and
ambiguity (the shared :class:`~xtreamscraper.core.matching.TitleMatcher`), merging, bindings,
artwork storage and NFO writing are done by the core. The plugin writes no files and never
reads Canal+ detail pages.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from xtreamscraper.core.matching import MatchDecision, TitleMatcher
from xtreamscraper.metadata.plugin import (
    Capability,
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
from xtreamscraper.utils.redact import redact

from .client import (
    CanalPlusAuthError,
    CanalPlusClient,
    CanalPlusError,
    CanalPlusFormatError,
    CanalPlusNotFound,
)
from .mapping import NAMESPACE, candidates, map_episode, map_movie, map_season, map_series
from .parsing import (
    EPISODE,
    MOVIE,
    OBJECT_ID,
    SEASON,
    SERIES,
    SHOW,
    SeriesFamily,
    build_family,
    canonical_path,
    is_trailer_page,
    kind_of,
    page_of,
    season_episode,
    series_root,
)
from .sites import DEFAULT, SITE_TAGS, SITES, site_for

log = logging.getLogger(__name__)

#: ``config -> client``; tests inject fakes here.
ClientFactory = Callable[[dict[str, Any]], CanalPlusClient]
SHOW_FILTER = f"type:{SHOW}"
FAMILY_FILTER = f"type:{SEASON} OR type:{EPISODE}"
SEARCH_PAGE_SIZE = 50
#: Search pages read per match attempt at most (pages 0-2).
MAX_SEARCH_PAGES = 3
FAMILY_PAGE_SIZE = 1000
#: Hard cap on family search pages; beyond it the family is incomplete.
MAX_FAMILY_PAGES = 10
PAGE_NAMESPACE = "canalplus_page"
KIND_LABEL = {MOVIE: "movie", SERIES: "series"}
RATE_LIMITED = 429
CHOOSE_LABEL = "Choose a country…"
NOT_CONFIGURED_MESSAGE = "Choose a country first"


def _default_client(config: dict[str, Any]) -> CanalPlusClient:
    return CanalPlusClient(site=site_for(config.get("country")))


def _searchable(title: Optional[str]) -> bool:
    return isinstance(title, str) and any(char.isalnum() for char in title)


class CanalPlusSession(ScraperSession):
    """One scrape job. Caches title records (and the IDs Canal+ says do not exist) and series families."""

    def __init__(self, client: CanalPlusClient, config: dict[str, Any], matcher: TitleMatcher) -> None:
        self.client = client
        self.site = site_for(config.get("country"))
        self.threshold = float(config.get("threshold") if config.get("threshold") is not None else 85)
        self.matcher = matcher
        self._records: dict[str, dict[str, Any]] = {}
        self._missing: set[str] = set()
        self._families: dict[str, SeriesFamily] = {}

    def close(self) -> None:
        self.client.close()

    def _suspend(self, message: str) -> str:
        """Stop using Canal+ for the rest of the job (the core skips it and warns once)."""
        if self.suspended is None:
            log.warning("Canal+ suspended for this job: %s", redact(message))
            self.suspended = message
        return message

    def _failure(self, exc: CanalPlusError) -> tuple[str, bool]:
        """``(message, format_error)`` for a client error; settings failures and a final 429 suspend
        the plugin for the job (a not-found is handled by the callers)."""
        if isinstance(exc, CanalPlusAuthError):
            return self._suspend(str(exc)), False
        if exc.status == RATE_LIMITED:
            return self._suspend("Canal+ rate limit reached; Canal+ is skipped for the rest of this job"), False
        return str(exc), isinstance(exc, CanalPlusFormatError)

    def _fetch_failure(self, exc: CanalPlusError) -> FetchOutcome:
        message, format_error = self._failure(exc)
        return FetchOutcome.provider_format_error(message) if format_error else FetchOutcome.error(message)

    # -- matching ------------------------------------------------------------------------------------
    def _wanted(self, hit: dict[str, Any], kind: str) -> bool:
        return kind_of(hit, self.site) == kind and not (kind == MOVIE and is_trailer_page(hit, self.site))

    def _decide(self, query: MatchQuery, kind: str) -> MatchDecision:
        """Search pages 0-2 of ``type:show`` records, keep those of ``kind`` and let the shared matcher
        decide over all kept records after each page. Stops at the first accepted match.

        Contract:
            Raises:
                CanalPlusError: any client failure (nothing is concluded).
        """
        pool: dict[str, dict[str, Any]] = {}
        year = query.year if kind == MOVIE else None
        decision: Optional[MatchDecision] = None
        for page in range(MAX_SEARCH_PAGES):
            result = self.client.search(query.title.strip(), SHOW_FILTER, page, SEARCH_PAGE_SIZE)
            for hit in result.hits:
                if self._wanted(hit, kind):
                    pool.setdefault(hit["objectID"], hit)
            decision = self.matcher.decide(query.title, year, candidates(pool.values(), kind), self.threshold)
            if decision.matched or result.nb_pages is None or page + 1 >= result.nb_pages or not result.hits:
                break
        assert decision is not None
        return decision

    def _match(self, query: MatchQuery, kind: str) -> MatchOutcome:
        """Title (and, for a movie, year) matching with the shared matcher.

        Contract:
            Silences:
                - Client errors become outcomes: format problems -> ``provider_format_error``;
                  settings failures and a final 429 suspend the plugin for the job and give
                  ``error``; any other failure -> ``error``. A title without letters or digits ->
                  ``unmatched`` without a request.
        """
        if not _searchable(query.title):
            return MatchOutcome.unmatched(message="no title to search for")
        try:
            decision = self._decide(query, kind)
        except CanalPlusError as exc:
            message, format_error = self._failure(exc)
            return MatchOutcome.provider_format_error(message) if format_error else MatchOutcome.error(message)
        best_year = decision.best.candidate.year if decision.best is not None else None
        method = MatchMethod.TITLE_YEAR if kind == MOVIE and query.year is not None and best_year is not None \
            else MatchMethod.TITLE_ONLY
        return MatchOutcome.from_decision(decision, method)

    def match_movie(self, query: MatchQuery) -> MatchOutcome:
        return self._match(query, MOVIE)

    def match_series(self, query: MatchQuery) -> MatchOutcome:
        return self._match(query, SERIES)

    # -- title records -------------------------------------------------------------------------------
    def _record(self, object_id: str) -> dict[str, Any]:
        """A record by ID, cached for the session (a missing one too).

        Contract:
            Raises:
                CanalPlusNotFound: Canal+ answered 404 (now or earlier in this session).
                CanalPlusFormatError: the answer is a record with another ``objectID``.
                CanalPlusError: other client failures (nothing cached).
        """
        if object_id in self._records:
            return self._records[object_id]
        if object_id in self._missing:
            raise CanalPlusNotFound("Not found at Canal+ (title record)", 404)
        try:
            record = self.client.get_object(object_id)
        except CanalPlusNotFound:
            self._missing.add(object_id)
            raise
        if record.get("objectID") != object_id:
            raise CanalPlusFormatError("Canal+ answered with another title record")
        self._records[object_id] = record
        return record

    def _show(self, remote_id: str, kind: str) -> tuple[Optional[dict[str, Any]], Optional[FetchOutcome]]:
        """The ``show`` record of ``kind`` for ``remote_id``, or the failure outcome (exactly one is set).

        Contract:
            Silences:
                - An ID of the wrong syntax, a 404, a season/episode record and a show of the other
                  kind -> ``not_found`` (never coerced to the parent show). Format problems ->
                  ``provider_format_error``; settings failures and a final 429 suspend the plugin
                  -> ``error``; other failures -> ``error`` (all keep the binding).
        """
        object_id = str(remote_id).strip()
        if not OBJECT_ID.fullmatch(object_id):
            return None, FetchOutcome.not_found("not a Canal+ ID")
        try:
            record = self._record(object_id)
        except CanalPlusNotFound:
            return None, FetchOutcome.not_found("not found at Canal+")
        except CanalPlusError as exc:
            return None, self._fetch_failure(exc)
        if record.get("type") != SHOW or kind_of(record, self.site) != kind:
            return None, FetchOutcome.not_found(f"Canal+ ID is not a {KIND_LABEL[kind]} of the requested kind")
        return record, None

    def get_movie(self, remote_id: str) -> FetchOutcome:
        record, failure = self._show(remote_id, MOVIE)
        if record is None:
            return failure or FetchOutcome.error("Canal+ title unavailable")
        return FetchOutcome.ok(map_movie(record))

    # -- series families -----------------------------------------------------------------------------
    def _family(self, show: dict[str, Any]) -> SeriesFamily:
        """Every season/episode record of a series, from title searches, cached for the session.

        Records are kept when their address is below the series' own address (Canal+'s search
        cannot filter on it). Pages are followed while they advance and stay below ``nbPages``,
        at most :data:`MAX_FAMILY_PAGES`. The family is marked incomplete when Canal+ reports more
        hits than could be read, the cap is reached or the pages stop advancing.

        Contract:
            Raises:
                CanalPlusFormatError: the series has no valid series address.
                CanalPlusError: client failures (nothing cached).
        """
        object_id = show["objectID"]
        if object_id in self._families:
            return self._families[object_id]
        root = series_root(show.get("url"), self.site)
        if root is None:
            raise CanalPlusFormatError("Canal+ series record has an unexpected page address")
        hits: dict[str, dict[str, Any]] = {}
        received = 0
        nb_hits: Optional[int] = None
        complete = True
        page = 0
        while True:
            result = self.client.search(show["title"].strip(), FAMILY_FILTER, page, FAMILY_PAGE_SIZE)
            if result.page is not None and result.page != page:
                complete = False
                break
            new = 0
            for hit in result.hits:
                if hit["objectID"] not in hits:
                    hits[hit["objectID"]] = hit
                    new += 1
            received += new + result.malformed
            nb_hits = result.nb_hits if result.nb_hits is not None else nb_hits
            if page > 0 and result.hits and new == 0:
                complete = False  # the same page again: no progress
                break
            if result.nb_pages is None:
                if nb_hits is None and len(result.hits) + result.malformed >= FAMILY_PAGE_SIZE:
                    complete = False
                break
            if page + 1 >= result.nb_pages:
                break
            if page + 1 >= MAX_FAMILY_PAGES:
                complete = False
                break
            page += 1
        if nb_hits is not None and nb_hits > received:
            complete = False
        if not complete:
            log.warning("Canal+ series %s: only %d of %s season/episode records could be read; missing seasons "
                        "and episodes are not treated as absent", object_id, received,
                        nb_hits if nb_hits is not None else "?")
        family = build_family(list(hits.values()), root, complete, self.site)
        self._families[object_id] = family
        return family

    def _series(self, remote_id: str) -> tuple[Optional[dict[str, Any]], Optional[SeriesFamily],
                                                Optional[FetchOutcome]]:
        record, failure = self._show(remote_id, SERIES)
        if record is None:
            return None, None, failure
        try:
            return record, self._family(record), None
        except CanalPlusError as exc:
            return record, None, self._fetch_failure(exc)

    def get_series(self, remote_id: str) -> FetchOutcome:
        record, family, failure = self._series(remote_id)
        if record is None or family is None:
            return failure or FetchOutcome.error("Canal+ series unavailable")
        return FetchOutcome.ok(map_series(record, family))

    def get_season(self, series_remote_id: str, season_number: int) -> FetchOutcome:
        """A season Canal+ lists through a season record or its episodes.

        Contract:
            Silences:
                - An unlisted season -> ``not_found`` for a complete family, ``error`` for an
                  incomplete one (its absence is not proven).
        """
        _record, family, failure = self._series(series_remote_id)
        if family is None:
            return failure or FetchOutcome.error("Canal+ series unavailable")
        if season_number not in family.seasons:
            if not family.complete:
                return FetchOutcome.error("Canal+ family result was incomplete; season absence cannot be confirmed")
            return FetchOutcome.not_found(f"Canal+ does not list season {season_number}")
        return FetchOutcome.ok(map_season(season_number, family.seasons[season_number]))

    def get_episode(self, series_remote_id: str, season_number: int, episode_number: int) -> FetchOutcome:
        """The episode with exactly this season and episode number (never a similar one).

        Contract:
            Silences:
                - A number listed by two records -> ``error`` (logged), never a guess. A missing
                  number -> ``not_found`` for a complete family, ``error`` for an incomplete one.
        """
        record, family, failure = self._series(series_remote_id)
        if record is None or family is None:
            return failure or FetchOutcome.error("Canal+ series unavailable")
        found = family.episodes.get((season_number, episode_number), [])
        if not found:
            if not family.complete:
                return FetchOutcome.error("Canal+ family result was incomplete; episode absence cannot be confirmed")
            return FetchOutcome.not_found(f"episode {season_number}x{episode_number} is not listed at Canal+")
        if len(found) > 1:
            log.warning("Canal+ series %s: episode %dx%d is listed by %d records (%s)", record["objectID"],
                        season_number, episode_number, len(found), ", ".join(hit["objectID"][:60] for hit in found))
            return FetchOutcome.error(f"Canal+ lists episode {season_number}x{episode_number} more than once")
        hit = found[0]
        if season_episode(hit, self.site) != (season_number, episode_number):  # pragma: no cover - grouped by these numbers
            return FetchOutcome.error("Canal+ episode numbers are inconsistent")
        meta = map_episode(hit, record, self.site)
        return FetchOutcome.ok(meta, remote_id=meta.remote_id)

    # -- manual IDs ----------------------------------------------------------------------------------
    def resolve_manual_id(self, media_type: str, namespace: str, value: str) -> ResolveOutcome:
        """A Canal+ page address (``film/<slug>`` / ``series/<slug>``) -> the ``show`` record's ID.

        Searches the slug's words (pages 0-2) and accepts only records whose canonical address is
        exactly that page and whose kind is ``media_type``; never a fuzzy fallback.

        Contract:
            Silences:
                - No exact record -> ``not_found``; two different exact records -> ``error``.
                  Client errors -> ``error`` (settings failures and a final 429 also suspend).
        """
        kinds = {"movie": ("film", MOVIE), "series": ("series", SERIES)}
        if namespace != PAGE_NAMESPACE or media_type not in kinds:
            return ResolveOutcome.unsupported()
        page_kind, kind = kinds[media_type]
        prefix, _, slug = value.partition("/")
        if prefix != page_kind or not slug:
            return ResolveOutcome.not_found(f"this is not a Canal+ {KIND_LABEL[kind]} page")
        wanted = f"{page_kind}/{slug}"
        exact: dict[str, dict[str, Any]] = {}
        try:
            for page in range(MAX_SEARCH_PAGES):
                result = self.client.search(slug.replace("-", " "), SHOW_FILTER, page, SEARCH_PAGE_SIZE)
                for hit in result.hits:
                    if hit.get("type") == SHOW and kind_of(hit, self.site) == kind and canonical_path(hit["url"], self.site) == wanted \
                            and not page_of(hit["url"], self.site).rest:  # type: ignore[union-attr]
                        exact.setdefault(hit["objectID"], hit)
                if result.nb_pages is None or page + 1 >= result.nb_pages or not result.hits:
                    break
        except CanalPlusError as exc:
            return ResolveOutcome.error(self._failure(exc)[0])
        if not exact:
            return ResolveOutcome.not_found("this page is not in the Canal+ search index")
        if len(exact) > 1:
            log.warning("Canal+ page %s is listed by %d records", wanted, len(exact))
            return ResolveOutcome.error(f"Canal+ lists this page {len(exact)} times; enter the Canal+ ID instead")
        return ResolveOutcome.ok(next(iter(exact)))

    # -- connection test -----------------------------------------------------------------------------
    def self_test(self) -> tuple[bool, str]:
        """Search settings and one search request (``hitsPerPage=1``). Writes nothing."""
        try:
            self.client.search_credentials()
        except CanalPlusError as exc:
            return False, f"Search settings: {redact(exc)}"
        try:
            self.client.search("", SHOW_FILTER, 0, 1)
        except CanalPlusError as exc:
            return False, f"Search index: {redact(exc)}"
        return True, f"Canal+ connection successful ({self.site.label}, search index reachable)"


class CanalPlusPlugin(ScraperPlugin):
    plugin_id = "canalplus"
    display_name = "Canal+"
    version = "20261015"
    capabilities = frozenset({
        Capability.MOVIES, Capability.SERIES, Capability.SEASONS, Capability.EPISODES,
        Capability.ARTWORK, Capability.EXTERNAL_IDS,
    })
    id_namespace = NAMESPACE

    def __init__(self, client_factory: Optional[ClientFactory] = None) -> None:
        self.client_factory = client_factory or _default_client

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField("country", "Country and language", FieldType.SELECT, default="", required=True,
                        choices=[("", CHOOSE_LABEL)] + [(site.tag, site.label) for site in SITES],
                        help="Required. The Canal+ website whose catalogue is searched. Canal+ titles and IDs "
                             "differ per country, so changing it matches every title again."),
            ConfigField("threshold", "Title match threshold", FieldType.INTEGER, default=85, minimum=0, maximum=100,
                        help="Minimum confidence (0–100) for a title match."),
        ]

    def manual_id_fields(self, media_type: str) -> list[ManualIdField]:
        if media_type not in ("movie", "series"):
            return []
        page = "film/the-runner" if media_type == "movie" else "series/snoepjes"
        return [
            ManualIdField(NAMESPACE, "Canal+ ID", help="The Canal+ search record ID (letters, digits, - and _).",
                          pattern_hint="letters, digits, - and _"),
            ManualIdField(PAGE_NAMESPACE, "Canal+ page address",
                          help="The title's page on the Canal+ website of the country set for the plugin "
                               "(e.g. canalplus.nl/stream); an episode page works for a series.",
                          placeholder=f"https://www.canalplus.nl/stream/{page}/"),
        ]

    def normalize_manual_id(self, namespace: str, value: str) -> str:
        """A bare Canal+ ID, or ``film/<slug>`` / ``series/<slug>`` from a page address.

        Contract:
            Raises:
                ManualIdError: another namespace, an ID with other characters or longer than 256,
                    or an address that is not a Canal+ film/series page (another host, userinfo, a
                    port, another path or an empty slug).
        """
        text = (value or "").strip()
        if namespace == NAMESPACE:
            if not OBJECT_ID.fullmatch(text):
                raise ManualIdError("A Canal+ ID has only letters, digits, - and _")
            return text
        if namespace != PAGE_NAMESPACE:
            raise ManualIdError("Canal+ only accepts Canal+ IDs and page addresses")
        if text.startswith(("film/", "series/", "/stream/", "stream/")):
            path = text.lstrip("/")
            text = f"https://{DEFAULT.host}/" + (path if path.startswith("stream/") else f"stream/{path}")
        path = next((found for site in SITES if (found := canonical_path(text, site)) is not None), None)
        if path is None:
            raise ManualIdError("Enter a Canal+ page address such as https://www.canalplus.nl/stream/series/snoepjes/")
        return path

    def validate_config(self, values: dict[str, Any]) -> dict[str, Any]:
        clean = validate_against_schema(self.config_schema(), values)
        if clean.get("threshold") is None:
            clean["threshold"] = 85
        return clean

    def is_configured(self, config: dict[str, Any]) -> bool:
        """Configured once a country is chosen (no account or key is needed); there is no default, so
        the plugin never searches a catalogue the user did not pick."""
        return config.get("country") in SITE_TAGS

    def test_connection(self, config: dict[str, Any]) -> TestResult:
        """Search settings discovery and one search request (see :meth:`CanalPlusSession.self_test`)."""
        if not self.is_configured(config):
            return False, NOT_CONFIGURED_MESSAGE
        session = CanalPlusSession(self.client_factory(config), config, TitleMatcher(self.scoring_profile))
        try:
            return session.self_test()
        finally:
            session.close()

    def create_session(self, config: dict[str, Any]) -> ScraperSession:
        return CanalPlusSession(self.client_factory(config), config, TitleMatcher(self.scoring_profile))
