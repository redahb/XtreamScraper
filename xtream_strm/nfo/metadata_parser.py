"""Reads the descriptive metadata of an NFO into a :class:`MetadataResult`.

The result is the "existing, protected" starting point of a scrape. Unknown tags are
ignored (and left in the file by the writer); malformed values only lose that value.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Optional, Union

from ..metadata.models import (
    Artwork,
    ArtworkType,
    Collection,
    MediaType,
    MetadataResult,
    PersonCredit,
    Rating,
)
from .document import NfoDocument
from .metadata_vocabulary import ASPECT_TO_TYPE, DATE_TAG
from .paths import NfoKind

KIND_TO_MEDIA = {
    NfoKind.MOVIE: MediaType.MOVIE,
    NfoKind.TVSHOW: MediaType.SERIES,
    NfoKind.SEASON: MediaType.SEASON,
    NfoKind.EPISODE: MediaType.EPISODE,
}


def _text(root: ET.Element, tag: str) -> Optional[str]:
    el = root.find(tag)
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def _int(value: Optional[str]) -> Optional[int]:
    try:
        number = int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and number >= 0 else None


def _float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _texts(root: ET.Element, tag: str) -> Optional[list[str]]:
    values = [e.text.strip() for e in root.findall(tag) if e.text and e.text.strip()]
    return values or None


def _people(root: ET.Element, tag: str) -> Optional[list[PersonCredit]]:
    people = [PersonCredit(name=e.text.strip()) for e in root.findall(tag) if e.text and e.text.strip()]
    return people or None


def _actors(root: ET.Element) -> Optional[list[PersonCredit]]:
    actors = []
    for el in root.findall("actor"):
        name = _text(el, "name")
        if not name:
            continue
        actors.append(PersonCredit(name=name, role=_text(el, "role"), order=_int(_text(el, "order")),
                                   profile_image=_text(el, "thumb")))
    return actors or None


def _artwork(root: ET.Element, kind: NfoKind) -> Optional[list[Artwork]]:
    art: list[Artwork] = []
    for el in root.findall("thumb"):
        url = (el.text or "").strip()
        if not url:
            continue
        aspect = (el.get("aspect") or "").strip().lower()
        season = _int(el.get("season"))
        if el.get("type") == "season" or (season is not None and kind is NfoKind.TVSHOW):
            art.append(Artwork(ArtworkType.SEASON_POSTER, url, season_number=season))
        elif not aspect and kind is NfoKind.EPISODE:
            art.append(Artwork(ArtworkType.STILL, url))
        elif aspect in ASPECT_TO_TYPE:
            art.append(Artwork(ASPECT_TO_TYPE[aspect], url))
    for el in root.findall("fanart/thumb"):
        url = (el.text or "").strip()
        if url:
            art.append(Artwork(ArtworkType.FANART, url))
    return art or None


def parse_metadata(source: Union[NfoDocument, ET.Element], kind: Optional[NfoKind] = None) -> MetadataResult:
    root = source.root if isinstance(source, NfoDocument) else source
    kind = kind or (source.kind if isinstance(source, NfoDocument) else None) or NfoKind.MOVIE
    result = MetadataResult(media_type=KIND_TO_MEDIA[kind])
    result.title = _text(root, "title")
    result.original_title = _text(root, "originaltitle")
    result.sort_title = _text(root, "sorttitle")
    result.plot = _text(root, "plot")
    result.outline = _text(root, "outline")
    result.tagline = _text(root, "tagline")
    result.year = _int(_text(root, "year")) or None
    result.premiered = _text(root, DATE_TAG[kind]) or (_text(root, "premiered") if kind is NfoKind.EPISODE else None)
    result.runtime_minutes = _int(_text(root, "runtime")) or None
    result.status = _text(root, "status")
    result.certification = _text(root, "mpaa")
    result.show_title = _text(root, "showtitle")
    result.genres = _texts(root, "genre")
    result.countries = _texts(root, "country")
    result.studios = _texts(root, "studio")
    result.tags = _texts(root, "tag")
    result.directors = _people(root, "director")
    result.writers = _people(root, "credits")
    result.actors = _actors(root)
    result.trailer = _text(root, "trailer")
    result.artwork = _artwork(root, kind)

    for el in root.findall("uniqueid"):
        id_type = (el.get("type") or "").strip().lower()
        value = (el.text or "").strip()
        if id_type and value:
            result.external_ids.setdefault(id_type, value)
            if (el.get("default") or "").lower() == "true" and result.default_id_type is None:
                result.default_id_type = id_type

    for el in root.findall("ratings/rating"):
        name = (el.get("name") or "").strip().lower()
        value = _float(_text(el, "value"))
        if not name or value is None or value <= 0:
            continue
        result.ratings.setdefault(name, Rating(value=value, votes=_int(_text(el, "votes")),
                                               max_value=_float(el.get("max")) or 10.0))
        if (el.get("default") or "").lower() == "true" and result.default_rating is None:
            result.default_rating = name

    set_el = root.find("set")
    if set_el is not None:
        name = _text(set_el, "name") or (set_el.text or "").strip()
        if name:
            result.collection = Collection(name=name, overview=_text(set_el, "overview"))

    if kind is NfoKind.SEASON:
        result.season_number = _int(_text(root, "seasonnumber"))
    elif kind is NfoKind.EPISODE:
        result.season_number = _int(_text(root, "season"))
        result.episode_number = _int(_text(root, "episode"))
    return result
