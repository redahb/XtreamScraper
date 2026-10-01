"""Remote artwork references in NFO files – the only code that adds or removes them.

A reference is an NFO location (``<thumb aspect="poster">``, ``<fanart><thumb>`` ...) plus
an exact URL. Removal only ever touches elements whose location *and* URL match, so user
or third-party artwork in the same NFO survives; the artwork manager only asks to remove
references it recorded as its own. Actor images live inside ``<actor>`` and are never
visible to this module. Only tags already in the core NFO vocabulary are used.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

from ..metadata.models import ArtworkType
from .document import NfoDocument
from .metadata_vocabulary import THUMB_ASPECT
from .paths import NfoKind


@dataclass(frozen=True)
class RefLocation:
    """Where in an NFO a reference lives. ``aspect == ""`` means a plain ``<thumb>``."""

    container: Optional[str] = None  # "fanart" or None (root level)
    aspect: str = ""
    season: Optional[int] = None  # tvshow.nfo season posters: type="season" season="N"

    @property
    def key(self) -> str:
        if self.container:
            return self.container
        text = f"thumb:{self.aspect}"
        return text + (f":season={self.season}" if self.season is not None else "")

    @classmethod
    def from_key(cls, key: str) -> "RefLocation":
        if key == "fanart":
            return cls(container="fanart")
        parts = key.split(":")
        season = None
        if len(parts) > 2 and parts[2].startswith("season="):
            season = int(parts[2][len("season="):])
        return cls(aspect=parts[1] if len(parts) > 1 else "", season=season)


def location_for(nfo_kind: NfoKind, art_type: ArtworkType, season: Optional[int] = None) -> Optional[RefLocation]:
    """The NFO location of ``art_type`` in an NFO of ``nfo_kind`` (None = not representable)."""
    art_type = ArtworkType(art_type)
    if art_type is ArtworkType.FANART:
        return RefLocation(container="fanart") if nfo_kind in (NfoKind.MOVIE, NfoKind.TVSHOW) else None
    if art_type is ArtworkType.SEASON_POSTER:
        if nfo_kind is NfoKind.TVSHOW and season is not None:
            return RefLocation(aspect="poster", season=season)
        return RefLocation(aspect="poster") if nfo_kind is NfoKind.SEASON else None
    if art_type is ArtworkType.EPISODE_STILL:
        return RefLocation(aspect="") if nfo_kind is NfoKind.EPISODE else None
    if art_type is ArtworkType.PERSON_IMAGE or art_type not in THUMB_ASPECT:
        return None
    return RefLocation(aspect=THUMB_ASPECT[art_type])


def _matches(el: ET.Element, loc: RefLocation) -> bool:
    aspect = (el.get("aspect") or "").strip().lower()
    is_season = (el.get("type") or "").lower() == "season" or el.get("season") is not None
    if loc.season is not None:
        return aspect == loc.aspect and is_season and (el.get("season") or "").strip() == str(loc.season)
    return aspect == loc.aspect and not is_season


def _elements(doc: NfoDocument, loc: RefLocation) -> list[tuple[ET.Element, ET.Element]]:
    """(parent, element) pairs at ``loc``."""
    if loc.container:
        return [(c, t) for c in doc.root.findall(loc.container) for t in c.findall("thumb")]
    return [(doc.root, t) for t in doc.root.findall("thumb") if _matches(t, loc)]


def _url(el: ET.Element) -> str:
    return (el.text or "").strip()


def find_urls(doc: NfoDocument, loc: RefLocation) -> list[str]:
    return [u for _, el in _elements(doc, loc) for u in [_url(el)] if u]


def _insert_position(root: ET.Element, tag: str) -> int:
    """After the last ``tag`` child, else before ``<fileinfo>``, else at the end."""
    children = list(root)
    last = max((i for i, c in enumerate(children) if c.tag == tag), default=None)
    if last is not None:
        return last + 1
    return next((i for i, c in enumerate(children) if c.tag == "fileinfo"), len(children))


def add_ref(doc: NfoDocument, loc: RefLocation, url: str) -> bool:
    """Add a reference unless the same URL is already at that location. Returns changed."""
    url = url.strip()
    if not url or url in find_urls(doc, loc):
        return False
    if loc.container:
        container = doc.root.find(loc.container)
        if container is None:
            container = ET.Element(loc.container)
            doc.root.insert(_insert_position(doc.root, "thumb"), container)
        ET.SubElement(container, "thumb").text = url
        return True
    attrs = {"aspect": loc.aspect} if loc.aspect else {}
    if loc.season is not None:
        attrs.update(type="season", season=str(loc.season))
    el = ET.Element("thumb", attrs)
    el.text = url
    doc.root.insert(_insert_position(doc.root, "thumb"), el)
    return True


def remove_ref(doc: NfoDocument, loc: RefLocation, url: str) -> bool:
    """Remove the reference(s) at ``loc`` with exactly this URL. Returns changed."""
    url = url.strip()
    changed = False
    for parent, el in _elements(doc, loc):
        if _url(el) == url:
            parent.remove(el)
            changed = True
    if changed and loc.container:
        for container in doc.root.findall(loc.container):
            if len(container) == 0 and not (container.text or "").strip():
                doc.root.remove(container)
    return changed
