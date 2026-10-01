"""Where artwork lives: canonical local file names and NFO locations per item and type.

Resolved centrally from ``artwork type + media item type + media path``; plugins never
choose file names. Extensions follow the downloaded image format (``.jpg`` for JPEG,
``.png`` for PNG...) because images are never converted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from ..config.settings import Settings
from ..filesystem import atomic
from ..library.models import LibraryItem, LibrarySeries
from ..metadata.models import ArtworkType
from ..nfo.artwork_refs import RefLocation, location_for
from ..nfo.paths import NfoKind, season_nfo_path, tvshow_nfo_path
from ..storage.artwork import ItemKey

MOVIE, SERIES, SEASON, EPISODE = "movie", "series", "season", "episode"

#: File extensions recognised as existing artwork (managed or not).
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".tbn")
FORMAT_EXTENSIONS = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif"}

_ITEM_ART = (ArtworkType.POSTER, ArtworkType.FANART, ArtworkType.CLEARLOGO, ArtworkType.BANNER, ArtworkType.LANDSCAPE,
             ArtworkType.KEYART)
#: Artwork types the manager handles per item kind.
TYPES_BY_KIND: dict[str, tuple[ArtworkType, ...]] = {
    MOVIE: _ITEM_ART,
    SERIES: _ITEM_ART,
    SEASON: (ArtworkType.SEASON_POSTER,),
    EPISODE: (ArtworkType.EPISODE_STILL,),
}
_ROOT_NAMES = {
    ArtworkType.POSTER: "poster",
    ArtworkType.FANART: "fanart",
    ArtworkType.CLEARLOGO: "clearlogo",
    ArtworkType.BANNER: "banner",
    ArtworkType.LANDSCAPE: "landscape",
    ArtworkType.KEYART: "keyart",  # its own file; never copied to or from poster
}


@dataclass(frozen=True)
class ArtworkTarget:
    """A library item artwork belongs to: stable IDs plus its current paths."""

    key: ItemKey
    title: str
    folder: str  # the media folder the artwork files go into
    nfo_path: str
    nfo_kind: NfoKind
    stem: Optional[str] = None  # episodes: the .strm base name
    season: Optional[int] = None
    parent_nfo_path: Optional[str] = None  # seasons: the show's tvshow.nfo

    @property
    def item_kind(self) -> str:
        return self.key[1]

    @property
    def label(self) -> str:
        return self.title

    @classmethod
    def for_movie(cls, item: LibraryItem) -> "ArtworkTarget":
        return cls((item.provider_id, MOVIE, item.category_id, item.item_id), item.label, item.folder,
                   item.nfo_path, NfoKind.MOVIE)

    @classmethod
    def for_series(cls, show: LibrarySeries) -> "ArtworkTarget":
        return cls((show.provider_id, SERIES, show.category_id, show.series_id), show.title, show.folder_path,
                   tvshow_nfo_path(show.folder_path), NfoKind.TVSHOW)

    @classmethod
    def for_season(cls, show: LibrarySeries, season_folder: str, season: int) -> "ArtworkTarget":
        return cls((show.provider_id, SEASON, show.category_id, f"{show.series_id}/{season}"),
                   f"{show.title} season {season}", season_folder, season_nfo_path(season_folder), NfoKind.SEASON,
                   season=season, parent_nfo_path=tvshow_nfo_path(show.folder_path))

    @classmethod
    def for_episode(cls, item: LibraryItem) -> "ArtworkTarget":
        stem = os.path.splitext(os.path.basename(item.strm_path))[0]
        return cls((item.provider_id, EPISODE, item.category_id, item.item_id), item.label, item.folder,
                   item.nfo_path, NfoKind.EPISODE, stem=stem, season=item.season)


def enabled_types(settings: Settings, item_kind: str) -> set[ArtworkType]:
    """Artwork types the user wants managed for this kind of item."""
    optional = {t for t, on in ((ArtworkType.CLEARLOGO, settings.artwork_clearlogo),
                                (ArtworkType.BANNER, settings.artwork_banner),
                                (ArtworkType.LANDSCAPE, settings.artwork_landscape),
                                (ArtworkType.KEYART, settings.artwork_keyart)) if on}
    if item_kind == MOVIE:
        base = {t for t, on in ((ArtworkType.POSTER, settings.artwork_movie_poster),
                                (ArtworkType.FANART, settings.artwork_movie_fanart)) if on}
        return base | optional
    if item_kind == SERIES:
        base = {t for t, on in ((ArtworkType.POSTER, settings.artwork_show_poster),
                                (ArtworkType.FANART, settings.artwork_show_fanart)) if on}
        return base | optional
    if item_kind == SEASON:
        return {ArtworkType.SEASON_POSTER} if settings.artwork_season_poster else set()
    if item_kind == EPISODE:
        return {ArtworkType.EPISODE_STILL} if settings.artwork_episode_still else set()
    return set()


@dataclass(frozen=True)
class LocalName:
    """A local artwork file name without extension (``.../poster``) and its role."""

    base: str
    role: str  # "primary" or "alias"


def local_names(target: ArtworkTarget, art_type: ArtworkType, aliases: bool = True) -> list[LocalName]:
    """Canonical local file names for ``art_type`` of ``target``; the primary name first."""
    art_type = ArtworkType(art_type)
    names: list[LocalName] = []
    if target.item_kind in (MOVIE, SERIES) and art_type in _ROOT_NAMES:
        names.append(LocalName(os.path.join(target.folder, _ROOT_NAMES[art_type]), "primary"))
    elif target.item_kind == SEASON and art_type is ArtworkType.SEASON_POSTER:
        names.append(LocalName(os.path.join(target.folder, "poster"), "primary"))
        names.append(LocalName(os.path.join(target.folder, f"Season{(target.season or 0):02d}"), "alias"))
    elif target.item_kind == EPISODE and art_type is ArtworkType.EPISODE_STILL and target.stem:
        names.append(LocalName(os.path.join(target.folder, f"{target.stem}-thumb"), "primary"))
        names.append(LocalName(os.path.join(target.folder, target.stem), "alias"))
    return names if aliases else names[:1]


def all_local_names(target: ArtworkTarget, art_type: ArtworkType) -> list[LocalName]:
    return local_names(target, art_type, aliases=True)


def existing_variants(base: str) -> list[str]:
    """Existing image files named ``base`` + any image extension (case-insensitive)."""
    directory, stem = os.path.split(base)
    try:
        entries = os.listdir(atomic.fs_path(directory))
    except OSError:
        return []
    folded = stem.casefold()
    return sorted(os.path.join(directory, e) for e in entries
                  if os.path.splitext(e)[0].casefold() == folded and os.path.splitext(e)[1].lower() in IMAGE_EXTENSIONS)


def nfo_locations(target: ArtworkTarget, art_type: ArtworkType) -> list[tuple[str, NfoKind, RefLocation]]:
    """NFO locations for a remote reference; the item's own NFO first."""
    result = []
    own = location_for(target.nfo_kind, art_type)
    if own is not None:
        result.append((target.nfo_path, target.nfo_kind, own))
    if target.item_kind == SEASON and target.parent_nfo_path and ArtworkType(art_type) is ArtworkType.SEASON_POSTER:
        parent = location_for(NfoKind.TVSHOW, art_type, target.season)
        if parent is not None:
            result.append((target.parent_nfo_path, NfoKind.TVSHOW, parent))
    return result
