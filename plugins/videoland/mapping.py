"""Conversion of Videoland data into shared match candidates and the normalized model.

Only what Videoland reliably exposes is mapped. Left unset on purpose (another plugin may fill
them through the core merge): year and premiere date of a programme, cast and crew, studios,
countries, networks, ratings and votes, the trailer (only its presence is known), season titles
and season artwork. Artwork is limited to the image ratios whose meaning was confirmed:
``2:3`` (poster), ``16:9`` (fanart), the hero's logo (clear logo) and an episode's own image.

Also here: signed image URLs (plain SHA1 of the path with its alphabetically sorted query,
followed by the image salt; the query is emitted in the hashed order), locale-independent
parsers for Videoland's Dutch runtime (``1uur18min``) and air date (``Zo 04 okt 26``) texts, and
the age-classification mapping (``CsaAl`` -> ``AL``).
"""

from __future__ import annotations

import datetime
import hashlib
import re
from typing import Any, Iterable, Optional, Union

from xtreamscraper.core.matching import MatchCandidate
from xtreamscraper.metadata.models import Artwork, ArtworkType, MediaType, MetadataResult
from xtreamscraper.utils.coerce import as_dict, as_list

from .client import CLIENT_CONFIG, ClientConfig, sorted_query
from .parsing import MOVIE, CatalogProgram, EpisodeStub, Layout

NAMESPACE = "videoland"
POSTER_RATIO, FANART_RATIO = "2:3", "16:9"
_IMAGE_ID = re.compile(r"[0-9]{1,12}")
_GENRE_SEPARATOR = "|"
#: Requested output sizes (not claims about the source resolution).
POSTER_SIZE = (1066, 1600)
FANART_SIZE = (1920, 1080)
CLEARLOGO_SIZE = (1600, 900)
EPISODE_STILL_SIZE = (1280, 720)
_RUNTIME = re.compile(r"\s*(?:(\d{1,3})\s*uur)?\s*(?:(\d{1,4})\s*min)?\s*", re.IGNORECASE)
_AIR_DATE = re.compile(r"\s*(?:(ma|di|wo|do|vr|za|zo)\.?\s+)?(\d{1,2})\s+([a-z]{3,4})\.?\s+(\d{2}|\d{4})\s*",
                       re.IGNORECASE)
MONTHS = {"jan": 1, "feb": 2, "mrt": 3, "apr": 4, "mei": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
          "okt": 10, "nov": 11, "dec": 12}
#: Two-digit years: 00-68 -> 2000-2068, 69-99 -> 1969-1999 (the conventional POSIX pivot).
YEAR_PIVOT = 69
_CSA = re.compile(r"Csa(Al|\d{1,2})")


# -- images ----------------------------------------------------------------------------------------
def build_image_url(image_id: Union[str, int], width: int, height: int, *, fit: str = "scale_crop", blur: int = 0,
                    interlace: int = 1, optimize: str = "high", image_format: str = "auto",
                    config: ClientConfig = CLIENT_CONFIG) -> str:
    """A signed image CDN URL; no token is needed to download it.

    The ``hash`` parameter is part of the URL's identity: it must never be stripped (the CDN
    answers 403 without a valid one). It is not known to expire.

    Example:
        >>> build_image_url("585754", 1066, 1600).endswith("hash=248839350575eb95993b0a19cc6126d04584b5fd")
        True
    """
    params = {"width": width, "height": height, "blur": blur, "fit": fit, "interlace": interlace,
              "format": image_format, "optimize": optimize}
    path = f"/v2/images/{image_id}/raw?{sorted_query(params)}"
    image_hash = hashlib.sha1((path + config.image_salt).encode("utf-8")).hexdigest()  # the CDN's scheme
    return f"{config.image_base}{path}&hash={image_hash}"


# -- Dutch texts -------------------------------------------------------------------------------------
def parse_runtime(text: Any) -> Optional[int]:
    """Minutes from ``1uur18min`` / ``1 uur 18 min`` / ``78min`` / ``1uur``; ``None`` otherwise.

    Example:
        >>> parse_runtime("1uur18min"), parse_runtime("45 min"), parse_runtime("vandaag")
        (78, 45, None)
    """
    if not isinstance(text, str):
        return None
    match = _RUNTIME.fullmatch(text)
    if not match or (match.group(1) is None and match.group(2) is None):
        return None
    minutes = int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
    return minutes if minutes > 0 else None


def expand_year(year: int) -> int:
    """Two-digit years with the fixed pivot (00-68 -> 20xx, 69-99 -> 19xx); others unchanged."""
    if year >= 100:
        return year
    return 2000 + year if year < YEAR_PIVOT else 1900 + year


def parse_air_date(text: Any) -> Optional[str]:
    """``Zo 04 okt 26`` -> ``2026-10-04``, read with fixed Dutch tables (no OS locale).

    The weekday is optional and informational: a weekday that does not match the date is not an
    error. Relative texts (``Vandaag``, ``Gisteren``) and anything else give ``None``.
    """
    if not isinstance(text, str):
        return None
    match = _AIR_DATE.fullmatch(text)
    if not match:
        return None
    month = MONTHS.get(match.group(3).lower())
    if month is None:
        return None
    try:
        return datetime.date(expand_year(int(match.group(4))), month, int(match.group(2))).isoformat()
    except ValueError:
        return None


def certification(content_advisory: Any) -> Optional[str]:
    """The age classification among ``contentAdvisory.pictos``: ``CsaAl`` -> ``AL``, ``Csa12`` -> ``12``.

    Content warnings (``ParentalGuidance*``) are never a certification; null entries are skipped.
    """
    for picto in as_list(as_dict(content_advisory).get("pictos")):
        name = as_dict(picto).get("name")
        match = _CSA.fullmatch(name) if isinstance(name, str) else None
        if match:
            code = match.group(1)
            return "AL" if code == "Al" else str(int(code))
    return None


# -- results ---------------------------------------------------------------------------------------
def catalog_candidates(programs: Iterable[CatalogProgram]) -> list[MatchCandidate]:
    """One candidate per programme: its ID, full title and (when plausible) production year."""
    return [MatchCandidate(remote_id=p.program_id, title=p.title, year=p.year) for p in programs]


def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def image_id(value: Any) -> Optional[str]:
    """A Videoland image ID (decimal digits, from text or a number), else ``None``."""
    if isinstance(value, bool):
        return None
    text = str(value).strip() if isinstance(value, (str, int)) else ""
    return text if _IMAGE_ID.fullmatch(text) else None


def _artwork(kind: ArtworkType, raw_id: Any, size: tuple[int, int]) -> Optional[Artwork]:
    found = image_id(raw_id)
    if found is None:
        return None
    width, height = size
    return Artwork(kind, build_image_url(found, width, height), width=width, height=height, source=NAMESPACE)


def genres(text: Any) -> Optional[list[str]]:
    """``extraDetails`` (``"Competitie | Lifestyle"``) as a list of genres, or ``None``."""
    value = _text(text)
    if value is None:
        return None
    found: list[str] = []
    for part in value.split(_GENRE_SEPARATOR):
        part = part.strip()
        if part and part not in found:
            found.append(part)
    return found or None


def layout_artwork(layout: Layout) -> Optional[list[Artwork]]:
    """Poster (``2:3``), fanart (``16:9``) and the hero's logo. Other ratios are not emitted:
    several are shared placeholders across programmes."""
    candidates = [
        _artwork(ArtworkType.POSTER, layout.image_ids.get(POSTER_RATIO), POSTER_SIZE),
        _artwork(ArtworkType.FANART, layout.image_ids.get(FANART_RATIO), FANART_SIZE),
        _artwork(ArtworkType.CLEARLOGO, as_dict(layout.hero.get("logo")).get("id"), CLEARLOGO_SIZE),
    ]
    found = [art for art in candidates if art is not None]
    return found or None


def map_program(layout: Layout, kind: str) -> MetadataResult:
    """A movie or series from its layout; the runtime is only read for a movie.

    Contract:
        Preconditions:
            - ``kind`` is ``MOVIE`` or ``SERIES`` (anything else maps as a series).
    """
    hero = layout.hero
    program_id = layout.program_id
    return MetadataResult(
        MediaType.MOVIE if kind == MOVIE else MediaType.SERIES,
        title=layout.title,
        plot=_text(hero.get("description")),
        genres=genres(hero.get("extraDetails")),
        certification=certification(hero.get("contentAdvisory")),
        runtime_minutes=parse_runtime(hero.get("extraHighlight")) if kind == MOVIE else None,
        external_ids={NAMESPACE: program_id},
        default_id_type=NAMESPACE,
        artwork=layout_artwork(layout),
        plugin_source=NAMESPACE,
        remote_id=program_id,
    )


def map_season(season_number: int) -> MetadataResult:
    """Only the structure: Videoland's season label is the standard ``Seizoen N``."""
    return MetadataResult(MediaType.SEASON, season_number=season_number, plugin_source=NAMESPACE)


def map_episode(stub: EpisodeStub, season_number: int) -> MetadataResult:
    """An episode from its stub; the programme's plot is never used for a missing episode plot."""
    content = stub.content
    still = _artwork(ArtworkType.EPISODE_STILL, as_dict(content.get("image")).get("id"), EPISODE_STILL_SIZE)
    return MetadataResult(
        MediaType.EPISODE,
        title=stub.title,
        season_number=season_number,
        episode_number=stub.number,
        plot=_text(content.get("description")),
        runtime_minutes=parse_runtime(content.get("highlight")),
        premiered=parse_air_date(content.get("details")),
        artwork=[still] if still else None,
        external_ids={NAMESPACE: stub.ucid} if stub.ucid else {},
        plugin_source=NAMESPACE,
        remote_id=stub.ucid,
    )
