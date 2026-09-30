"""Serializes a :class:`MetadataResult` into an NFO document.

The only code that writes descriptive-metadata XML. It replaces a tag group only when
the model holds a meaningful value for it, so nothing is ever removed because a value is
missing. ``<fileinfo>`` and tags outside the managed vocabulary are never touched.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Optional

from ..metadata.merge import KEY_FUNCTIONS, dedupe, meaningful
from ..metadata.models import ArtworkType, MetadataResult, PersonCredit
from .document import NfoDocument
from .metadata_vocabulary import DATE_TAG, MANAGED_ASPECTS, THUMB_ASPECT
from .paths import NfoKind


def _set(doc: NfoDocument, tag: str, value: object) -> None:
    if meaningful(value) or (isinstance(value, int) and not isinstance(value, bool) and value == 0
                             and tag in ("season", "seasonnumber")):
        doc.set_text(tag, value if not isinstance(value, float) else f"{value:g}")


def _set_list(doc: NfoDocument, tag: str, values: Optional[list[str]]) -> None:
    clean = dedupe(values or [], KEY_FUNCTIONS["genres"])
    if clean:
        doc.set_list(tag, clean)


def _set_people(doc: NfoDocument, tag: str, people: Optional[list[PersonCredit]], key: str) -> None:
    clean = dedupe(people or [], KEY_FUNCTIONS[key])
    if clean:
        doc.set_list(tag, [p.name for p in clean])


def _actors(doc: NfoDocument, actors: Optional[list[PersonCredit]]) -> None:
    clean = dedupe(actors or [], KEY_FUNCTIONS["actors"])
    if not clean:
        return
    elements = []
    for index, person in enumerate(clean):
        el = ET.Element("actor")
        ET.SubElement(el, "name").text = person.name
        if meaningful(person.role):
            ET.SubElement(el, "role").text = person.role
        ET.SubElement(el, "order").text = str(person.order if person.order is not None else index)
        if meaningful(person.profile_image):
            ET.SubElement(el, "thumb").text = person.profile_image
        elements.append(el)
    doc.replace_elements("actor", elements)


def _unique_ids(doc: NfoDocument, meta: MetadataResult) -> None:
    ids = [(k, v) for k, v in meta.external_ids.items() if k and meaningful(v)]
    if not ids:
        return
    default = meta.default_id_type if meta.default_id_type in dict(ids) else None
    elements = []
    for id_type, value in ids:
        el = ET.Element("uniqueid", {"type": id_type})
        if id_type == default:
            el.set("default", "true")
        el.text = str(value)
        elements.append(el)
    doc.replace_elements("uniqueid", elements)


def _ratings(doc: NfoDocument, meta: MetadataResult) -> None:
    ratings = [(k, r) for k, r in meta.ratings.items() if k and meaningful(r)]
    if not ratings:
        return
    default = meta.default_rating if meta.default_rating in dict(ratings) else None
    container = ET.Element("ratings")
    for name, rating in ratings:
        el = ET.SubElement(container, "rating", {"name": name, "max": f"{rating.max_value:g}"})
        if name == default:
            el.set("default", "true")
        ET.SubElement(el, "value").text = f"{rating.value:g}"
        if rating.votes:
            ET.SubElement(el, "votes").text = str(rating.votes)
    doc.replace_elements("ratings", [container])


def _artwork(doc: NfoDocument, meta: MetadataResult, kind: NfoKind) -> None:
    art = dedupe(meta.artwork or [], KEY_FUNCTIONS["artwork"])
    thumbs = [a for a in art if a.type is not ArtworkType.FANART]
    fanart = [a for a in art if a.type is ArtworkType.FANART]
    if thumbs:
        position = next((i for i, c in enumerate(doc.root) if c.tag == "thumb"), None)
        for old in [c for c in doc.root.findall("thumb") if (c.get("aspect") or "").lower() in MANAGED_ASPECTS]:
            doc.root.remove(old)
        new = []
        for a in thumbs:
            if a.type is ArtworkType.SEASON_POSTER and kind is NfoKind.TVSHOW:
                el = ET.Element("thumb", {"aspect": "poster", "type": "season", "season": str(a.season_number or 0)})
            elif a.type is ArtworkType.STILL and kind is not NfoKind.EPISODE:
                el = ET.Element("thumb", {"aspect": "landscape"})
            else:
                aspect = THUMB_ASPECT.get(a.type, "")
                el = ET.Element("thumb", {"aspect": aspect} if aspect else {})
            el.text = a.url
            new.append(el)
        if position is None or position > len(doc.root):
            doc.root.extend(new)
        else:
            for offset, el in enumerate(new):
                doc.root.insert(position + offset, el)
    if fanart:
        container = ET.Element("fanart")
        for a in fanart:
            ET.SubElement(container, "thumb").text = a.url
        doc.replace_elements("fanart", [container])


def apply_metadata(doc: NfoDocument, meta: MetadataResult) -> None:
    """Write ``meta`` into ``doc``. ``<fileinfo>`` is never touched."""
    kind = doc.kind or NfoKind.MOVIE
    _set(doc, "title", meta.title)
    _set(doc, "originaltitle", meta.original_title)
    _set(doc, "sorttitle", meta.sort_title)
    if kind is NfoKind.EPISODE:
        _set(doc, "showtitle", meta.show_title)
        _set(doc, "season", meta.season_number)
        _set(doc, "episode", meta.episode_number)
    if kind is NfoKind.SEASON:
        _set(doc, "seasonnumber", meta.season_number)
    _set(doc, "plot", meta.plot)
    _set(doc, "outline", meta.outline)
    _set(doc, "tagline", meta.tagline)
    if kind is not NfoKind.EPISODE:
        _set(doc, "year", meta.year)
    _set(doc, DATE_TAG[kind], meta.premiered)
    _set(doc, "runtime", meta.runtime_minutes)
    _set(doc, "mpaa", meta.certification)
    if kind is NfoKind.TVSHOW:
        _set(doc, "status", meta.status)
    _set_list(doc, "genre", meta.genres)
    _set_list(doc, "country", meta.countries)
    _set_list(doc, "studio", (meta.studios or []) + (meta.networks or []))
    _set_list(doc, "tag", meta.tags)
    _set_people(doc, "director", meta.directors, "directors")
    _set_people(doc, "credits", meta.writers, "writers")
    if meta.collection is not None and meaningful(meta.collection.name) and kind is NfoKind.MOVIE:
        el = ET.Element("set")
        ET.SubElement(el, "name").text = meta.collection.name
        if meaningful(meta.collection.overview):
            ET.SubElement(el, "overview").text = meta.collection.overview
        doc.replace_elements("set", [el])
    _ratings(doc, meta)
    _unique_ids(doc, meta)
    _actors(doc, meta.actors)
    _artwork(doc, meta, kind)
    _set(doc, "trailer", meta.trailer)
