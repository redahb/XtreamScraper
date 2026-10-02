"""Serializes a :class:`MetadataResult` into an NFO document.

The only code that writes descriptive-metadata XML. It replaces a tag group only when
the model holds a meaningful value for it, so nothing is ever removed because a value is
missing, except in a rebuild (``clear_missing``), where the managed tags the model has no
value for are removed. ``<fileinfo>`` and tags outside the managed vocabulary are never touched.

Media artwork (``<thumb>`` / ``<fanart>`` at NFO level) is not written here: the artwork
manager decides whether artwork becomes a local file or a remote reference and writes
references through :mod:`.artwork_refs`. Actor ``<thumb>`` images are part of ``<actor>``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Optional

from ..metadata.merge import KEY_FUNCTIONS, dedupe, meaningful
from ..metadata.models import MetadataResult, PersonCredit
from .document import NfoDocument
from .metadata_vocabulary import DATE_TAG
from .paths import NfoKind


def _set(doc: NfoDocument, tag: str, value: object, clear: bool = False) -> None:
    if meaningful(value) or (isinstance(value, int) and not isinstance(value, bool) and value == 0
                             and tag in ("season", "seasonnumber")):
        doc.set_text(tag, value if not isinstance(value, float) else f"{value:g}")
    elif clear:
        doc.remove(tag)


def _set_list(doc: NfoDocument, tag: str, values: Optional[list[str]], clear: bool = False) -> None:
    clean = dedupe(values or [], KEY_FUNCTIONS["genres"])
    if clean:
        doc.set_list(tag, clean)
    elif clear:
        doc.remove(tag)


def _set_people(doc: NfoDocument, tag: str, people: Optional[list[PersonCredit]], key: str,
                clear: bool = False) -> None:
    clean = dedupe(people or [], KEY_FUNCTIONS[key])
    if clean:
        doc.set_list(tag, [p.name for p in clean])
    elif clear:
        doc.remove(tag)


def _actors(doc: NfoDocument, actors: Optional[list[PersonCredit]], clear: bool = False) -> None:
    clean = dedupe(actors or [], KEY_FUNCTIONS["actors"])
    if not clean:
        if clear:
            doc.remove("actor")
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


def _unique_ids(doc: NfoDocument, meta: MetadataResult, clear: bool = False) -> None:
    ids = [(k, v) for k, v in meta.external_ids.items() if k and meaningful(v)]
    if not ids:
        if clear:
            doc.remove("uniqueid")
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


def _ratings(doc: NfoDocument, meta: MetadataResult, clear: bool = False) -> None:
    ratings = [(k, r) for k, r in meta.ratings.items() if k and meaningful(r)]
    if not ratings:
        if clear:
            doc.remove("ratings")
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


def apply_metadata(doc: NfoDocument, meta: MetadataResult, clear_missing: bool = False) -> None:
    """Write ``meta`` into ``doc``. ``<fileinfo>`` and media artwork are never touched.

    ``clear_missing`` rebuilds the descriptive part: managed tags ``meta`` has no value for
    are removed instead of kept (used once after a manual match replaced a wrong one)."""
    c = clear_missing
    kind = doc.kind or NfoKind.MOVIE
    _set(doc, "title", meta.title, c)
    _set(doc, "originaltitle", meta.original_title, c)
    _set(doc, "sorttitle", meta.sort_title, c)
    if kind is NfoKind.EPISODE:
        _set(doc, "showtitle", meta.show_title, c)
        _set(doc, "season", meta.season_number)
        _set(doc, "episode", meta.episode_number)
    if kind is NfoKind.SEASON:
        _set(doc, "seasonnumber", meta.season_number)
    _set(doc, "plot", meta.plot, c)
    _set(doc, "outline", meta.outline, c)
    _set(doc, "tagline", meta.tagline, c)
    if kind is not NfoKind.EPISODE:
        _set(doc, "year", meta.year, c)
    _set(doc, DATE_TAG[kind], meta.premiered, c)
    _set(doc, "runtime", meta.runtime_minutes, c)
    _set(doc, "mpaa", meta.certification, c)
    if kind is NfoKind.TVSHOW:
        _set(doc, "status", meta.status, c)
    _set_list(doc, "genre", meta.genres, c)
    _set_list(doc, "country", meta.countries, c)
    _set_list(doc, "studio", (meta.studios or []) + (meta.networks or []), c)
    _set_list(doc, "tag", meta.tags, c)
    _set_people(doc, "director", meta.directors, "directors", c)
    _set_people(doc, "credits", meta.writers, "writers", c)
    if meta.collection is not None and meaningful(meta.collection.name) and kind is NfoKind.MOVIE:
        el = ET.Element("set")
        ET.SubElement(el, "name").text = meta.collection.name
        if meaningful(meta.collection.overview):
            ET.SubElement(el, "overview").text = meta.collection.overview
        doc.replace_elements("set", [el])
    elif c and kind is NfoKind.MOVIE:
        doc.remove("set")
    _ratings(doc, meta, c)
    _unique_ids(doc, meta, c)
    _actors(doc, meta.actors, c)
    _set(doc, "trailer", meta.trailer, c)


def apply_manual_ids(doc: NfoDocument, assign: dict[str, str], remove: dict[str, str]) -> None:
    """IDs the user set or withdrew by hand: ``<uniqueid>`` of each ``remove`` type is dropped only
    while it still holds exactly that value; each ``assign`` value replaces its type's value."""
    for id_type, value in remove.items():
        for el in list(doc.root.findall("uniqueid")):
            if (el.get("type") or "").strip().lower() == id_type.lower() \
                    and (el.text or "").strip().casefold() == str(value).strip().casefold():
                doc.root.remove(el)
    for id_type, value in assign.items():
        doc.set_uniqueid(id_type, value)
