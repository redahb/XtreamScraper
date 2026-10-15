"""Reading Videoland's public catalog pages and programme layouts (no I/O).

Everything here is defensive: Videoland's web-client data is undocumented. A shape this module
does not recognise raises :class:`~.client.VideolandFormatError` (never "not found"), and only
an explicit "this programme does not exist" answer raises
:class:`~.client.VideolandNotFound`. Blocks are recognised by structure and titles, never by
position.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from xtreamscraper.core.matching.normalization import normalize_title
from xtreamscraper.utils.coerce import as_dict, as_list

from .client import VideolandFormatError, VideolandNotFound

log = logging.getLogger(__name__)

#: Catalog buckets: one per letter, plus ``@`` for everything else.
LETTERS: tuple[str, ...] = tuple("abcdefghijklmnopqrstuvwxyz") + ("@",)
OTHER_BUCKET = "@"
MOVIE, SERIES = "movie", "series"
DEHYDRATED_MARKER = "root.__dehydratedState = "
LETTERS_QUERY = ["programs", "letters"]
BY_LETTER_QUERY = ["programs", "byLetter"]
#: Videoland's ``year_production``: only plausible years are used (for matching only).
MIN_YEAR, MAX_YEAR = 1888, 2100

SEASON_TITLE = re.compile(r"Seizoen\s+(\d{1,4})")
EPISODE_TITLE = re.compile(r"(\d{1,6})\.\s*(.*)", re.DOTALL)
TRAILERS_TITLE = "Trailers"
RECOMMENDATIONS_TITLE = "Anderen kijken ook"
PAGINATED, PARALLAX = "bffPaginated", "parallax"



# -- catalog -----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CatalogProgram:
    """One programme of a catalog page: its stable numeric ID (as text), title and, when given,
    a plausible production year (used for matching only, never written as metadata)."""

    program_id: str
    title: str
    year: Optional[int] = None


def bucket_for_title(title: Optional[str]) -> Optional[str]:
    """The catalog bucket a title is expected in: its first letter after the shared title
    normalization (accents folded, case folded), or ``@`` for a digit or another character.

    ``None`` when the title has no letter or digit at all. Videoland files a few programmes
    under their URL slug's letter instead (e.g. "De AI Storm" under ``a``); the session falls
    back to the whole catalog when the bucket gives no match.
    """
    for char in normalize_title(title):
        if char.isalnum():
            return char if "a" <= char <= "z" else OTHER_BUCKET
    return None


def extract_dehydrated_state(html: str) -> dict[str, Any]:
    """The ``root.__dehydratedState = {...}`` object of a server-rendered page.

    Decoded with a real JSON decoder from the marker on, never by searching a closing brace.

    Contract:
        Raises:
            VideolandFormatError: no marker, invalid JSON after it, or not a JSON object.
    """
    index = html.find(DEHYDRATED_MARKER)
    if index < 0:
        raise VideolandFormatError("Videoland catalog page format changed (no dehydrated state)")
    try:
        state, _end = json.JSONDecoder().raw_decode(html, index + len(DEHYDRATED_MARKER))
    except ValueError:
        raise VideolandFormatError("Videoland catalog page format changed (dehydrated state is not valid JSON)") \
            from None
    if not isinstance(state, dict):
        raise VideolandFormatError("Videoland catalog page format changed (dehydrated state is not an object)")
    return state


def _queries(state: dict[str, Any]) -> list[dict[str, Any]]:
    queries = as_dict(state.get("__tanstackQueryState")).get("queries")
    if not isinstance(queries, list):
        raise VideolandFormatError("Videoland catalog page format changed (no query list)")
    return [q for q in queries if isinstance(q, dict)]


def find_query(state: dict[str, Any], key: list[Any]) -> Optional[dict[str, Any]]:
    """The first query whose ``queryKey`` starts with ``key`` (found by content, not position)."""
    for query in _queries(state):
        query_key = query.get("queryKey")
        if isinstance(query_key, list) and query_key[:len(key)] == key:
            return query
    return None


def _query_data(query: dict[str, Any]) -> Any:
    return as_dict(query.get("state")).get("data")


def letter_counts(state: dict[str, Any]) -> dict[str, int]:
    """``programCountsByLetter``: bucket -> number of programmes.

    Contract:
        Raises:
            VideolandFormatError: no letters query, or no ``programCountsByLetter`` object.
        Silences:
            - Entries that are not a known bucket with a non-negative integer count are dropped.
    """
    query = find_query(state, LETTERS_QUERY)
    counts = as_dict(_query_data(query)).get("programCountsByLetter") if query else None
    if not isinstance(counts, dict):
        raise VideolandFormatError("Videoland catalog page format changed (no programme counts)")
    return {letter: count for letter, count in counts.items()
            if letter in LETTERS and isinstance(count, int) and not isinstance(count, bool) and count >= 0}


def production_year(value: Any) -> Optional[int]:
    """``year_production`` (``"2002"`` or ``2002``) when it is a plausible year, else ``None``."""
    if isinstance(value, str) and len(value.strip()) == 4 and value.strip().isdecimal():
        value = int(value.strip())
    if isinstance(value, int) and not isinstance(value, bool) and MIN_YEAR <= value <= MAX_YEAR:
        return value
    return None


def page_programs(state: dict[str, Any], letter: str, page: int) -> Optional[list[CatalogProgram]]:
    """The programmes of catalog page ``(letter, page)``; ``None`` when the page has no such query.

    Contract:
        Raises:
            VideolandFormatError: the query exists but its data is not a list.
        Silences:
            - Entries without an integer ``id`` or a text ``title`` are skipped (counted in a
              debug log line). ``year_production`` is kept only when plausible.
    """
    query = find_query(state, BY_LETTER_QUERY + [letter, page])
    if query is None:
        return None
    data = _query_data(query)
    if not isinstance(data, list):
        raise VideolandFormatError("Videoland catalog page format changed (programme list)")
    programs: list[CatalogProgram] = []
    for entry in data:
        program = as_dict(entry)
        program_id, title = program.get("id"), program.get("title")
        if isinstance(program_id, bool) or not isinstance(program_id, int) or program_id <= 0 \
                or not isinstance(title, str) or not title.strip():
            continue
        programs.append(CatalogProgram(str(program_id), title.strip(), production_year(program.get("year_production"))))
    if len(programs) != len(data):
        log.debug("Videoland catalog %s/%d: %d entries without an ID or title skipped", letter, page,
                  len(data) - len(programs))
    return programs


# -- layout ------------------------------------------------------------------------------------------
@dataclass
class SeasonBlock:
    """A ``Seizoen N`` block: its number, full block ``id`` (for pagination) and first items."""

    number: int
    block_id: str
    items: list[Any]
    next_page: Any = None
    total_items: Optional[int] = None


@dataclass
class Layout:
    """The parts of a programme layout (all its block pages) the plugin uses."""

    program_id: str
    title: Optional[str]
    hero: dict[str, Any]
    image_ids: dict[str, str]
    seasons: list[SeasonBlock] = field(default_factory=list)
    has_trailers: bool = False


def check_layout_page(data: Any, program_id: str) -> dict[str, Any]:
    """Validate one layout page and return it.

    Contract:
        Raises:
            VideolandNotFound: the answer is a valid layout of something else. Videoland answers an
                unknown programme with its landing page (HTTP 200, ``entity.type`` ``frontspace``).
            VideolandFormatError: ``entity``, ``seo``, ``blocks`` or ``pagination`` missing or of
                the wrong type, or an entity without a type.
    """
    page = as_dict(data)
    entity = page.get("entity")
    if not isinstance(entity, dict) or not isinstance(page.get("seo"), dict) \
            or not isinstance(page.get("blocks"), list) or not isinstance(page.get("pagination"), dict):
        raise VideolandFormatError("Videoland programme layout format changed or could not be parsed")
    kind = entity.get("type")
    if not isinstance(kind, str) or not kind:
        raise VideolandFormatError("Videoland programme layout format changed (no entity type)")
    if kind != "program" or str(entity.get("id")) != str(program_id):
        log.info("Videoland programme %s: answered with %s %r instead", program_id, kind[:20],
                 str(entity.get("id"))[:20])
        raise VideolandNotFound(f"Videoland programme {program_id} does not exist")
    return page


def next_page(pagination: Any) -> Any:
    """The raw ``nextPage`` value (validated by the caller)."""
    return as_dict(pagination).get("nextPage")


def valid_page_number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _block_title(content: dict[str, Any]) -> Any:
    title = content.get("title")
    if title is None:
        return None
    short = as_dict(title).get("short")
    return short.strip() if isinstance(short, str) else short


def parse_layout(pages: list[dict[str, Any]], program_id: str) -> Layout:
    """The programme's title, hero item, image IDs and season blocks from its layout pages.

    ``pages`` are validated pages (see :func:`check_layout_page`), the first one first. The hero
    is the ``bffPaginated`` block of the first page without a title and with exactly one item.

    Contract:
        Raises:
            VideolandFormatError: no hero, more than one, or a hero item without ``itemContent``.
        Silences:
            - Advertising (``parallax``), recommendations ("Anderen kijken ook") and unknown blocks
              are ignored; titled blocks that look like a season but do not match ``Seizoen N``
              exactly are skipped with an info log line.
            - Non-object blocks and blocks without a ``content`` object are skipped.
    """
    first = pages[0]
    seo = as_dict(first.get("seo"))
    title = seo.get("title")
    if not isinstance(title, str) or not title.strip():
        title = as_dict(as_dict(first.get("entity")).get("metadata")).get("title")
    image_ids = {str(ratio): str(image_id) for ratio, image_id in
                 as_dict(as_dict(seo.get("image")).get("idsByRatio")).items()
                 if isinstance(image_id, (str, int)) and not isinstance(image_id, bool)}
    heroes: list[dict[str, Any]] = []
    seasons: list[SeasonBlock] = []
    has_trailers = False
    for page_number, page in enumerate(pages, start=1):
        for block in as_list(page.get("blocks")):
            if not isinstance(block, dict) or block.get("type") != PAGINATED or not isinstance(block.get("content"), dict):
                continue
            content = block["content"]
            block_title = _block_title(content)
            items = content.get("items")
            if block_title is None:
                if page_number == 1 and isinstance(items, list) and len(items) == 1:
                    heroes.append(as_dict(items[0]))
                continue
            if not isinstance(block_title, str):
                continue
            season = SEASON_TITLE.fullmatch(block_title)
            if season:
                block_id = block.get("id")
                if not isinstance(block_id, str) or not block_id or not isinstance(items, list):
                    raise VideolandFormatError("Videoland programme layout format changed (season block)")
                pagination = as_dict(content.get("pagination"))
                total = pagination.get("totalItems")
                seasons.append(SeasonBlock(int(season.group(1)), block_id, items, pagination.get("nextPage"),
                                           total if valid_page_number(total) else None))
            elif block_title == TRAILERS_TITLE:
                has_trailers = True  # presence only: no verified way to play a trailer yet
            elif block_title.lower().startswith(("seizoen", "special")):
                log.info("Videoland programme %s: block %r skipped (not a verified season label)", program_id,
                         block_title[:40])
    if len(heroes) != 1:
        raise VideolandFormatError(f"Videoland programme layout format changed ({len(heroes)} main items)")
    hero = heroes[0].get("itemContent")
    if not isinstance(hero, dict):
        raise VideolandFormatError("Videoland programme layout format changed (main item)")
    return Layout(str(program_id), title.strip() if isinstance(title, str) and title.strip() else None,
                  hero, image_ids, seasons, has_trailers)


def classify(layout: Layout) -> str:
    """``series`` when the layout has at least one ``Seizoen N`` block, else ``movie``.

    This is Videoland's observed web layout, not a formal content type: kept in one place so it
    can be replaced if Videoland exposes a better signal. The hero's season-count text
    (``2 seizoenen``) is deliberately not used.
    """
    return SERIES if layout.seasons else MOVIE


# -- episodes ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class EpisodeStub:
    """One numbered episode of a season block."""

    number: int
    title: Optional[str]
    ucid: Optional[str]
    content: dict[str, Any]


def parse_episode_title(text: Any) -> Optional[tuple[int, Optional[str]]]:
    """``"23. 20th Birthday Party"`` -> ``(23, "20th Birthday Party")``; ``None`` without a number."""
    if not isinstance(text, str):
        return None
    match = EPISODE_TITLE.fullmatch(text.strip())
    if not match:
        return None
    title = match.group(2).strip()
    return int(match.group(1)), title or None


def episode_stubs(items: list[Any], context: str) -> list[EpisodeStub]:
    """Numbered episode stubs from season block items, deduplicated by ``ucid`` and sorted by number.

    Contract:
        Silences:
            - Items without ``itemContent``, or whose ``extraTitle`` has no ``N.`` number, are
              skipped (one info log line with the count); no number is ever invented.
            - A repeated ``ucid`` keeps the first item. Items without a ``ucid`` are all kept.
            - The sort is stable, so equal numbers keep their arrival order.
    """
    stubs: list[EpisodeStub] = []
    seen: set[str] = set()
    unnumbered = 0
    for item in items:
        entry = as_dict(item)
        content = entry.get("itemContent")
        if not isinstance(content, dict):
            unnumbered += 1
            continue
        parsed = parse_episode_title(content.get("extraTitle"))
        if parsed is None:
            unnumbered += 1
            continue
        ucid = entry.get("ucid")
        ucid = ucid.strip() if isinstance(ucid, str) and ucid.strip() else None
        if ucid is not None:
            if ucid in seen:
                continue
            seen.add(ucid)
        stubs.append(EpisodeStub(parsed[0], parsed[1], ucid, content))
    if unnumbered:
        log.info("Videoland %s: %d episode item(s) without an episode number skipped", context, unnumbered)
    return sorted(stubs, key=lambda stub: stub.number)
