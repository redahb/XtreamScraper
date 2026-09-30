"""Renders normalised metadata models into NFO documents.

Only fields with known values are replaced. ``<fileinfo>`` is never touched here, so
probe-derived stream details survive metadata updates. Values keyed by a source
(``<uniqueid type=...>``, ``<rating name=...>``, ``<thumb aspect=...>``) are replaced
per source, so several metadata providers can contribute to one NFO.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Iterable, Optional

from ..metadata.models import (
    Artwork,
    EpisodeMetadata,
    ExternalIds,
    MovieMetadata,
    Person,
    Rating,
    SeasonMetadata,
    TvShowMetadata,
)
from .document import NfoDocument


def _actors(doc: NfoDocument, people: Optional[list[Person]]) -> None:
    if people is None:
        return
    elements = []
    for index, person in enumerate(people):
        if not person.name:
            continue
        actor = ET.Element("actor")
        ET.SubElement(actor, "name").text = person.name
        if person.role:
            ET.SubElement(actor, "role").text = person.role
        ET.SubElement(actor, "order").text = str(person.order if person.order is not None else index)
        if person.thumb:
            ET.SubElement(actor, "thumb").text = person.thumb
        elements.append(actor)
    doc.replace_elements("actor", elements)


def _ratings(doc: NfoDocument, ratings: Optional[list[Rating]]) -> None:
    if not ratings:
        return
    container = doc.find_or_create("ratings")
    for rating in ratings:
        for old in [r for r in container.findall("rating") if (r.get("name") or "").lower() == rating.source.lower()]:
            container.remove(old)
        element = ET.SubElement(container, "rating", {"name": rating.source, "max": f"{rating.max_value:g}"})
        if rating.default:
            for other in container.findall("rating"):
                other.attrib.pop("default", None)
            element.set("default", "true")
        ET.SubElement(element, "value").text = f"{rating.value:g}"
        if rating.votes is not None:
            ET.SubElement(element, "votes").text = str(rating.votes)


def _ids(doc: NfoDocument, ids: ExternalIds, default_order: Iterable[str]) -> None:
    items = ids.items()
    if not items:
        return
    present = {k for k, _ in items}
    default_type = next((t for t in default_order if t in present), None)
    for id_type, value in items:
        doc.set_uniqueid(id_type, value, default=id_type == default_type)
    if ids.imdb:
        doc.set_text("imdbid", ids.imdb)
    if ids.tmdb:
        doc.set_text("tmdbid", ids.tmdb)
    if ids.tvdb:
        doc.set_text("tvdbid", ids.tvdb)


def _artwork(doc: NfoDocument, artwork: Optional[list[Artwork]]) -> None:
    if not artwork:
        return
    thumbs = [a for a in artwork if a.kind != "fanart"]
    fanart = [a for a in artwork if a.kind == "fanart"]
    replaced: set[tuple[str, Optional[int]]] = set()
    for art in thumbs:
        key = (art.kind, art.season)
        if key not in replaced:
            for old in list(doc.root.findall("thumb")):
                season = old.get("season")
                if old.get("aspect") == art.kind and (season == (str(art.season) if art.season is not None else None)):
                    doc.root.remove(old)
            replaced.add(key)
        attrs = {"aspect": art.kind}
        if art.season is not None:
            attrs.update(type="season", season=str(art.season))
        if art.preview:
            attrs["preview"] = art.preview
        element = ET.Element("thumb", attrs)
        element.text = art.url
        doc.root.append(element)
    if fanart:
        container = ET.Element("fanart")
        for art in fanart:
            thumb = ET.SubElement(container, "thumb")
            if art.preview:
                thumb.set("preview", art.preview)
            thumb.text = art.url
        doc.replace_elements("fanart", [container])


def _year(value: Optional[int]) -> Optional[str]:
    return str(value) if value else None


def apply_movie_metadata(doc: NfoDocument, meta: MovieMetadata) -> None:
    doc.set_text("title", meta.title)
    doc.set_text("originaltitle", meta.original_title)
    doc.set_text("sorttitle", meta.sort_title)
    doc.set_text("year", _year(meta.year))
    doc.set_text("premiered", meta.premiered)
    doc.set_text("plot", meta.plot)
    doc.set_text("outline", meta.outline)
    doc.set_text("tagline", meta.tagline)
    doc.set_text("runtime", meta.runtime_minutes)
    doc.set_text("mpaa", meta.mpaa)
    doc.set_list("genre", meta.genres)
    doc.set_list("studio", meta.studios)
    doc.set_list("country", meta.countries)
    doc.set_list("director", meta.directors)
    doc.set_list("credits", meta.writers)
    doc.set_list("tag", meta.tags)
    if meta.collection:
        collection = ET.Element("set")
        ET.SubElement(collection, "name").text = meta.collection
        doc.replace_elements("set", [collection])
    _ratings(doc, meta.ratings)
    _ids(doc, meta.ids, ("tmdb", "imdb", "tvdb"))
    _actors(doc, meta.actors)
    _artwork(doc, meta.artwork)


def apply_tvshow_metadata(doc: NfoDocument, meta: TvShowMetadata) -> None:
    doc.set_text("title", meta.title)
    doc.set_text("originaltitle", meta.original_title)
    doc.set_text("year", _year(meta.year))
    doc.set_text("premiered", meta.premiered)
    doc.set_text("plot", meta.plot)
    doc.set_text("status", meta.status)
    doc.set_text("mpaa", meta.mpaa)
    doc.set_list("genre", meta.genres)
    doc.set_list("studio", meta.studios)
    _ratings(doc, meta.ratings)
    _ids(doc, meta.ids, ("tvdb", "tmdb", "imdb"))
    _actors(doc, meta.actors)
    _artwork(doc, meta.artwork)


def apply_season_metadata(doc: NfoDocument, meta: SeasonMetadata) -> None:
    doc.set_text("seasonnumber", meta.season_number)
    doc.set_text("title", meta.title)
    doc.set_text("plot", meta.plot)
    doc.set_text("premiered", meta.premiered)
    _ids(doc, meta.ids, ("tvdb", "tmdb"))
    _artwork(doc, meta.artwork)


def apply_episode_metadata(doc: NfoDocument, meta: EpisodeMetadata) -> None:
    doc.set_text("title", meta.title)
    doc.set_text("season", meta.season)
    doc.set_text("episode", meta.episode)
    doc.set_text("plot", meta.plot)
    doc.set_text("aired", meta.aired)
    doc.set_text("runtime", meta.runtime_minutes)
    doc.set_list("director", meta.directors)
    doc.set_list("credits", meta.writers)
    _ratings(doc, meta.ratings)
    _ids(doc, meta.ids, ("tvdb", "tmdb", "imdb"))
    _actors(doc, meta.actors)
    _artwork(doc, meta.artwork)
