"""Videoland scraper plugin.

Responsibilities: Videoland request behaviour (anonymous token, catalog pages, layouts),
candidate retrieval from the public catalog, confirming each candidate's media type, and
mapping to the normalized model. Scoring, thresholds and ambiguity (the shared
:class:`~xtreamscraper.core.matching.TitleMatcher`), merging, bindings, artwork storage and NFO
writing are done by the core. The plugin never resolves playback streams.

Videoland's token service wants an ``x-auth-device-id``. Like the web client (one per browser),
the plugin keeps one ``_luid_<uuid4>`` per installation in ``device_id.txt``. It is not an
account and not a secret.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import uuid
from typing import Any, Callable, Optional

from xtreamscraper.core.matching import ScoredCandidate, TitleMatcher
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
from xtreamscraper.filesystem.atomic import atomic_write_text
from xtreamscraper.metadata.results import FetchOutcome, MatchMethod, MatchOutcome, MatchQuery, MatchStatus
from xtreamscraper.utils.redact import redact

from .client import (
    VideolandAuthError,
    VideolandClient,
    VideolandError,
    VideolandFormatError,
    VideolandNotFound,
)
from .mapping import catalog_candidates, map_episode, map_program, map_season
from .parsing import (
    LETTERS,
    MOVIE,
    SERIES,
    CatalogProgram,
    EpisodeStub,
    Layout,
    SeasonBlock,
    bucket_for_title,
    check_layout_page,
    classify,
    episode_stubs,
    extract_dehydrated_state,
    letter_counts,
    next_page,
    page_programs,
    parse_layout,
    valid_page_number,
)

log = logging.getLogger(__name__)

#: ``(config, device_id) -> client``; tests inject fakes here.
ClientFactory = Callable[[dict[str, Any], str], VideolandClient]
#: At most this many ranked candidates get their media type confirmed per match attempt.
MAX_TYPE_CHECKS = 5
#: Hard caps on followed pages (a loop or a runaway answer is a format error, never endless).
MAX_LAYOUT_PAGES = 10
MAX_BLOCK_REQUESTS = 100
MAX_CATALOG_PAGES = 50
TEST_LETTER = "a"
_PROGRAM_ID = re.compile(r"[0-9]{1,12}")
_PAGE_ADDRESS = re.compile(r"-p_([0-9]{1,12})(?:[/?#]|$)")
KIND_LABEL = {MOVIE: "movie", SERIES: "series"}
#: The installation's anonymous device ID (Redah's choice: next to the plugin's own files, so
#: deleting the plugin folder deletes it too; git-ignored, written atomically).
DEVICE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device_id.txt")
_DEVICE_ID = re.compile(r"_luid_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


# -- device ID -----------------------------------------------------------------------------------
def new_device_id() -> str:
    return f"_luid_{uuid.uuid4()}"


def is_device_id(value: str) -> bool:
    return _DEVICE_ID.fullmatch(value) is not None


def load_or_create_device_id(path: str = DEVICE_FILE) -> str:
    """The device ID stored at ``path``; a new one is generated and saved when there is none.

    Contract:
        Silences:
            - A missing, unreadable or malformed file is replaced by a new ID (a malformed or
              unreadable one is logged).
            - When the new ID cannot be saved (read-only folder...), a warning is logged and the
              ID is still returned: it is then used until the application stops.
    """
    try:
        with open(path, encoding="ascii") as handle:
            stored = handle.read(200).strip()
    except FileNotFoundError:
        stored = ""
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("Videoland device ID file could not be read (%s); a new ID is created", type(exc).__name__)
        stored = ""
    if is_device_id(stored):
        return stored
    if stored:
        log.warning("Videoland device ID file holds an unexpected value; a new ID is created")
    value = new_device_id()
    try:
        atomic_write_text(path, value + "\n")
    except OSError as exc:
        log.warning("Videoland device ID could not be saved (%s); it is used until the application stops",
                    type(exc).__name__)
    return value


def _default_client(config: dict[str, Any], device_id: str) -> VideolandClient:
    return VideolandClient(device_id)


class VideolandSession(ScraperSession):
    """One scrape job. Caches catalog buckets, programme layouts and season episode lists."""

    def __init__(self, client: VideolandClient, config: dict[str, Any], matcher: TitleMatcher) -> None:
        self.client = client
        self.threshold = float(config.get("threshold") if config.get("threshold") is not None else 85)
        self.matcher = matcher
        self._pages: dict[tuple[str, int], dict[str, Any]] = {}  # (bucket, page) -> dehydrated state
        self._counts: Optional[dict[str, int]] = None
        self._buckets: dict[str, list[CatalogProgram]] = {}
        self._layouts: dict[str, Layout] = {}
        self._missing: set[str] = set()  # programme IDs Videoland says do not exist
        self._episodes: dict[tuple[str, str], list[EpisodeStub]] = {}

    def close(self) -> None:
        self.client.close()

    def _suspend(self, exc: VideolandAuthError) -> str:
        """Stop using Videoland for the rest of the job (the core skips it and warns once)."""
        message = str(exc)
        if self.suspended is None:
            log.warning("Videoland suspended for this job: %s", redact(message))
            self.suspended = message
        return message

    # -- catalog -------------------------------------------------------------------------------------
    def _catalog_state(self, letter: str, page: int) -> dict[str, Any]:
        """A catalog page's dehydrated state, fetched once per session.

        Contract:
            Raises:
                VideolandFormatError: from :func:`extract_dehydrated_state`; also for a 404 on the
                    first page of a bucket (the catalog address scheme changed).
                VideolandNotFound: a 404 on a later page.
                VideolandError: other request failures (nothing is cached).
        """
        key = (letter, page)
        if key not in self._pages:
            try:
                html = self.client.catalog_page(letter, page)
            except VideolandNotFound:
                if page == 1:
                    raise VideolandFormatError("Videoland catalog page address changed (HTTP 404)") from None
                raise
            state = extract_dehydrated_state(html)
            if self._counts is None:
                self._counts = letter_counts(state)
            self._pages[key] = state
        return self._pages[key]

    def _bucket(self, letter: str) -> list[CatalogProgram]:
        """Every programme of one catalog bucket (all its pages), cached for the session.

        Contract:
            Raises:
                VideolandFormatError: page 1 lacks the bucket's programme list although the bucket
                    has programmes, a later page lacks it, or the page count exceeds
                    :data:`MAX_CATALOG_PAGES`.
                VideolandError: request failures.
            Silences:
                - A 404 on a later page ends the bucket early (Videoland's count may have shrunk);
                  a total that differs from the count is logged, not an error.
        """
        if letter in self._buckets:
            return self._buckets[letter]
        state = self._catalog_state(letter, 1)
        counts = self._counts if self._counts is not None else letter_counts(state)
        count = counts.get(letter, 0)
        first = page_programs(state, letter, 1)
        if first is None:
            if count:
                raise VideolandFormatError(f"Videoland catalog page format changed (no list for {letter!r})")
            first = []
        programs = {p.program_id: p for p in first}
        if count > len(first):
            if not first:
                raise VideolandFormatError(f"Videoland catalog page format changed (empty first page for {letter!r})")
            pages = math.ceil(count / len(first))
            if pages > MAX_CATALOG_PAGES:
                raise VideolandFormatError(f"Videoland catalog bucket {letter!r} has too many pages ({pages})")
            for page in range(2, pages + 1):
                try:
                    found = page_programs(self._catalog_state(letter, page), letter, page)
                except VideolandNotFound:
                    log.info("Videoland catalog %s: page %d of %d does not exist", letter, page, pages)
                    break
                if found is None:
                    raise VideolandFormatError(f"Videoland catalog page format changed (no list for {letter!r} "
                                               f"page {page})")
                for program in found:
                    programs.setdefault(program.program_id, program)
        if len(programs) != count:
            log.info("Videoland catalog %s: %d programmes read, %d announced", letter, len(programs), count)
        self._buckets[letter] = list(programs.values())
        return self._buckets[letter]

    def _all_programs(self) -> list[CatalogProgram]:
        """The whole catalog (every bucket, each fetched once per session)."""
        programs: dict[str, CatalogProgram] = {}
        for letter in LETTERS:
            for program in self._bucket(letter):
                programs.setdefault(program.program_id, program)
        return list(programs.values())

    # -- layouts -------------------------------------------------------------------------------------
    def _layout(self, program_id: str) -> Layout:
        """The parsed layout of a programme (all block pages), cached for the session.

        Contract:
            Raises:
                VideolandNotFound: the programme does not exist (remembered for the session).
                VideolandFormatError: an unrecognised layout, or layout pagination that loops, is
                    not a positive integer or exceeds :data:`MAX_LAYOUT_PAGES`.
                VideolandAuthError, VideolandError: request failures.
        """
        if program_id in self._layouts:
            return self._layouts[program_id]
        if program_id in self._missing:
            raise VideolandNotFound(f"Videoland programme {program_id} does not exist")
        try:
            pages = [check_layout_page(self.client.layout(program_id, 1), program_id)]
        except VideolandNotFound:
            self._missing.add(program_id)
            raise
        visited = {1}
        following = next_page(pages[0].get("pagination"))
        while following is not None:
            if not valid_page_number(following) or following in visited or len(visited) >= MAX_LAYOUT_PAGES:
                log.warning("Videoland programme %s: layout pagination stopped at %r", program_id, str(following)[:20])
                raise VideolandFormatError("Videoland programme layout pagination is inconsistent")
            visited.add(following)
            page = check_layout_page(self.client.layout(program_id, following), program_id)
            pages.append(page)
            following = next_page(page.get("pagination"))
        layout = parse_layout(pages, program_id)
        self._layouts[program_id] = layout
        return layout

    def _season_episodes(self, program_id: str, season: SeasonBlock) -> list[EpisodeStub]:
        """Every numbered episode of a season block, following its pagination safely.

        Contract:
            Raises:
                VideolandFormatError: a follow-up page that is not a block with an item list, a
                    repeated or invalid ``nextPage``, or more than :data:`MAX_BLOCK_REQUESTS` pages.
                VideolandAuthError, VideolandError: request failures (nothing cached).
            Silences:
                - ``totalItems`` is only compared with the result (logged when different).
        """
        key = (program_id, season.block_id)
        if key in self._episodes:
            return self._episodes[key]
        items = list(season.items)
        visited: set[int] = set()
        requests_made = 0
        following = season.next_page
        while following is not None:
            if not valid_page_number(following) or following in visited or requests_made >= MAX_BLOCK_REQUESTS:
                log.warning("Videoland programme %s season %d: episode pagination stopped at %r", program_id,
                            season.number, str(following)[:20])
                raise VideolandFormatError("Videoland season episode pagination is inconsistent")
            visited.add(following)
            requests_made += 1
            block = self.client.block(program_id, season.block_id, following)
            content = block.get("content")
            if not isinstance(content, dict) or not isinstance(content.get("items"), list):
                raise VideolandFormatError("Videoland season block format changed or could not be parsed")
            items.extend(content["items"])
            following = next_page(content.get("pagination"))
        stubs = episode_stubs(items, f"programme {program_id} season {season.number}")
        if season.total_items is not None and len(items) != season.total_items:
            log.info("Videoland programme %s season %d: %d items read, %d announced", program_id, season.number,
                     len(items), season.total_items)
        self._episodes[key] = stubs
        return stubs

    # -- matching ------------------------------------------------------------------------------------
    def _decide(self, query: MatchQuery, kind: str, programs: list[CatalogProgram]) -> MatchOutcome:
        """Rank every programme with the shared matcher, confirm the media type of the best ones
        and let the shared matcher decide over those of the wanted ``kind``.

        At most :data:`MAX_TYPE_CHECKS` candidates scoring at least ``threshold -
        ambiguity_margin`` are confirmed; a candidate below that can neither be accepted nor make
        an accepted one ambiguous.

        Contract:
            Raises:
                VideolandAuthError, VideolandError: a candidate's layout request failed (other than
                    "does not exist"); the match is then not concluded.
            Silences:
                - A candidate that does not exist is skipped. One whose layout is not understood is
                  skipped too; the result is a format error only when no candidate of ``kind`` was
                  confirmed and at least one was skipped for that.
        """
        ranked = self.matcher.rank(query.title, query.year, catalog_candidates(programs))
        floor = self.threshold - self.matcher.profile.ambiguity_margin
        kept: list[ScoredCandidate] = []
        format_problem: Optional[VideolandFormatError] = None
        for scored in ranked[:MAX_TYPE_CHECKS]:
            if scored.score < floor:
                break
            program_id = str(scored.candidate.remote_id)
            try:
                found = classify(self._layout(program_id))
            except VideolandNotFound:
                log.info("Videoland candidate %s skipped: the programme does not exist", program_id)
                continue
            except VideolandFormatError as exc:
                log.info("Videoland candidate %s skipped: %s", program_id, redact(exc))
                format_problem = format_problem or exc
                continue
            if found == kind:
                kept.append(scored)
        if not kept and format_problem is not None:
            return MatchOutcome.provider_format_error(str(format_problem))
        decision = self.matcher.decide_ranked(kept, self.threshold)
        best_year = decision.best.candidate.year if decision.best is not None else None
        method = MatchMethod.TITLE_YEAR if query.year is not None and best_year is not None else MatchMethod.TITLE_ONLY
        outcome = MatchOutcome.from_decision(decision, method)
        if not kept and ranked and ranked[0].score >= floor:
            outcome.message = f"no Videoland {KIND_LABEL[kind]} among the best catalog matches"
        return outcome

    def _match(self, query: MatchQuery, kind: str) -> MatchOutcome:
        """Match in the title's catalog bucket; when that gives no match, in the whole catalog.

        Videoland files a few programmes under their URL slug's letter, not their title's, so a
        title missing from its own bucket is looked for in every bucket (each page is fetched at
        most once per job).

        Contract:
            Silences:
                - Client errors become outcomes: format problems -> ``provider_format_error``;
                  token failures suspend the plugin for the job and give ``error``; any other
                  failure -> ``error``. A title without letters or digits -> ``unmatched``.
        """
        letter = bucket_for_title(query.title)
        if letter is None:
            return MatchOutcome.unmatched(message="no title to search for")
        try:
            outcome = self._decide(query, kind, self._bucket(letter))
            if outcome.status is MatchStatus.UNMATCHED:
                outcome = self._decide(query, kind, self._all_programs())
        except VideolandAuthError as exc:
            return MatchOutcome.error(self._suspend(exc))
        except VideolandFormatError as exc:
            return MatchOutcome.provider_format_error(str(exc))
        except VideolandError as exc:
            return MatchOutcome.error(str(exc))
        return outcome

    def match_movie(self, query: MatchQuery) -> MatchOutcome:
        return self._match(query, MOVIE)

    def match_series(self, query: MatchQuery) -> MatchOutcome:
        return self._match(query, SERIES)

    # -- movies and series ---------------------------------------------------------------------------
    def _program(self, remote_id: str, kind: str) -> tuple[Optional[Layout], Optional[FetchOutcome]]:
        """The layout of a programme of ``kind``, or the failure outcome (exactly one is set).

        Contract:
            Silences:
                - A remote ID that is not a Videoland programme ID, a programme that does not exist,
                  and one of the other kind -> ``not_found``. Format problems ->
                  ``provider_format_error``; token failures suspend the plugin -> ``error``; other
                  failures -> ``error`` (all three keep the binding).
        """
        program_id = str(remote_id).strip()
        if not _PROGRAM_ID.fullmatch(program_id):
            return None, FetchOutcome.not_found("not a Videoland programme ID")
        try:
            layout = self._layout(program_id)
        except VideolandNotFound:
            return None, FetchOutcome.not_found("not found at Videoland")
        except VideolandAuthError as exc:
            return None, FetchOutcome.error(self._suspend(exc))
        except VideolandFormatError as exc:
            return None, FetchOutcome.provider_format_error(str(exc))
        except VideolandError as exc:
            return None, FetchOutcome.error(str(exc))
        if classify(layout) != kind:
            return None, FetchOutcome.not_found(f"the Videoland programme is not a {KIND_LABEL[kind]}")
        return layout, None

    def _fetch_program(self, remote_id: str, kind: str) -> FetchOutcome:
        layout, failure = self._program(remote_id, kind)
        if layout is None:
            return failure or FetchOutcome.error("Videoland programme unavailable")
        meta = map_program(layout, kind)
        return FetchOutcome.ok(meta, remote_id=layout.program_id)

    def get_movie(self, remote_id: str) -> FetchOutcome:
        return self._fetch_program(remote_id, MOVIE)

    def get_series(self, remote_id: str) -> FetchOutcome:
        return self._fetch_program(remote_id, SERIES)

    # -- seasons and episodes ------------------------------------------------------------------------
    def _season(self, series_remote_id: str, season_number: int
                ) -> tuple[Optional[Layout], Optional[SeasonBlock], Optional[FetchOutcome]]:
        """The season block Videoland exposes for ``season_number``, or the failure outcome.

        Videoland's layout does not always list every season of a long-running show; a season it
        does not list is ``not_found`` for this plugin only (a coverage gap, never a reason to
        doubt the local season). Two blocks with the same number -> ``error`` (never a guess).
        """
        layout, failure = self._program(series_remote_id, SERIES)
        if layout is None:
            return None, None, failure
        blocks = [block for block in layout.seasons if block.number == season_number]
        if not blocks:
            log.info("Videoland does not expose season %d of programme %s in its current layout", season_number,
                     layout.program_id)
            return layout, None, FetchOutcome.not_found(f"Videoland does not expose season {season_number} in its "
                                                        "current programme layout")
        if len(blocks) > 1:
            log.warning("Videoland programme %s lists season %d twice", layout.program_id, season_number)
            return layout, None, FetchOutcome.error(f"Videoland lists season {season_number} twice")
        return layout, blocks[0], None

    def get_season(self, series_remote_id: str, season_number: int) -> FetchOutcome:
        _, block, failure = self._season(series_remote_id, season_number)
        if block is None:
            return failure or FetchOutcome.error("Videoland season unavailable")
        return FetchOutcome.ok(map_season(season_number))

    def get_episode(self, series_remote_id: str, season_number: int, episode_number: int) -> FetchOutcome:
        """The episode with exactly this number in the exposed season (never a similar one).

        Contract:
            Silences:
                - An episode number listed by two different episodes -> ``error`` (logged), never
                  a guess. A missing number -> ``not_found``.
        """
        layout, block, failure = self._season(series_remote_id, season_number)
        if layout is None or block is None:
            return failure or FetchOutcome.error("Videoland season unavailable")
        try:
            stubs = self._season_episodes(layout.program_id, block)
        except VideolandAuthError as exc:
            return FetchOutcome.error(self._suspend(exc))
        except VideolandFormatError as exc:
            return FetchOutcome.provider_format_error(str(exc))
        except VideolandError as exc:
            return FetchOutcome.error(str(exc))
        found = [stub for stub in stubs if stub.number == episode_number]
        if not found:
            return FetchOutcome.not_found(f"episode {season_number}x{episode_number} is not listed at Videoland")
        if len(found) > 1:
            log.warning("Videoland programme %s season %d: episode %d is listed %d times", layout.program_id,
                        season_number, episode_number, len(found))
            return FetchOutcome.error(f"Videoland lists episode {season_number}x{episode_number} more than once")
        meta = map_episode(found[0], season_number)
        return FetchOutcome.ok(meta, remote_id=meta.remote_id)

    # -- connection test -----------------------------------------------------------------------------
    def self_test(self) -> tuple[bool, str]:
        """Token, one public catalog page and the layout of its first programme.

        Writes nothing (no metadata, no bindings).
        """
        try:
            self.client.jwt()
        except VideolandError as exc:
            return False, f"Anonymous token: {exc}"
        try:
            programs = self._bucket(TEST_LETTER)
        except VideolandError as exc:
            return False, f"Public catalog: {exc}"
        if not programs:
            return False, "Public catalog: Videoland listed no programmes"
        program = programs[0]
        try:
            layout = self._layout(program.program_id)
        except VideolandError as exc:
            return False, f"Programme layout: {exc}"
        return True, (f"Videoland connection successful ({len(programs)} programmes under "
                      f"{TEST_LETTER.upper()}; {program.title}: {KIND_LABEL[classify(layout)]})")


class VideolandPlugin(ScraperPlugin):
    plugin_id = "videoland"
    display_name = "Videoland"
    version = "20261008"
    capabilities = frozenset({
        Capability.MOVIES, Capability.SERIES, Capability.SEASONS, Capability.EPISODES,
        Capability.ARTWORK, Capability.EXTERNAL_IDS,
    })
    id_namespace = "videoland"

    def __init__(self, client_factory: Optional[ClientFactory] = None, device_file: str = DEVICE_FILE) -> None:
        self.client_factory = client_factory or _default_client
        self.device_file = device_file
        self._device_id: Optional[str] = None
        self._device_lock = threading.Lock()

    def device_id(self) -> str:
        """The installation's device ID, read (or created) on first use only."""
        with self._device_lock:
            if self._device_id is None:
                self._device_id = load_or_create_device_id(self.device_file)
            return self._device_id

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField("threshold", "Title match threshold", FieldType.INTEGER, default=85, minimum=0, maximum=100,
                        help="Minimum confidence (0–100) for a title match."),
        ]

    def manual_id_fields(self, media_type: str) -> list[ManualIdField]:
        if media_type not in ("movie", "series"):
            return []
        return [ManualIdField("videoland", "Videoland ID", help="The number at the end of the Videoland page "
                              "address, after -p_. The page address works too.", placeholder="147",
                              pattern_hint="number")]

    def normalize_manual_id(self, namespace: str, value: str) -> str:
        """A bare programme ID from an ID or a page address (``.../8-mile-p_147`` -> ``147``).

        Contract:
            Raises:
                ManualIdError: another namespace, or no 1-12 digit ID (explicit).
        """
        if namespace != "videoland":
            raise ManualIdError("Videoland only accepts Videoland IDs")
        text = (value or "").strip()
        address = _PAGE_ADDRESS.search(text)
        if address:
            text = address.group(1)
        if not _PROGRAM_ID.fullmatch(text):
            raise ManualIdError("A Videoland ID is the number after -p_ in the page address")
        return str(int(text))

    def validate_config(self, values: dict[str, Any]) -> dict[str, Any]:
        clean = validate_against_schema(self.config_schema(), values)
        if clean.get("threshold") is None:
            clean["threshold"] = 85
        return clean

    def is_configured(self, config: dict[str, Any]) -> bool:
        return True  # no account or key needed

    def test_connection(self, config: dict[str, Any]) -> TestResult:
        """Token, public catalog parsing and one programme layout (see :meth:`VideolandSession.self_test`)."""
        session = VideolandSession(self.client_factory(config, self.device_id()), config,
                                   TitleMatcher(self.scoring_profile))
        try:
            return session.self_test()
        finally:
            session.close()

    def create_session(self, config: dict[str, Any]) -> ScraperSession:
        return VideolandSession(self.client_factory(config, self.device_id()), config,
                                TitleMatcher(self.scoring_profile))
