"""Mapping between the normalized metadata model and Kodi/Jellyfin NFO tags.

Shared by the descriptive-metadata parser and writer so both always agree.
"""

from __future__ import annotations

from ..metadata.models import ArtworkType
from .paths import NfoKind

# Tag used for the premiere date per NFO type.
DATE_TAG = {
    NfoKind.MOVIE: "premiered",
    NfoKind.TVSHOW: "premiered",
    NfoKind.SEASON: "premiered",
    NfoKind.EPISODE: "aired",
}

# <thumb aspect="..."> values for artwork types written as root-level <thumb> elements.
THUMB_ASPECT = {
    ArtworkType.POSTER: "poster",
    ArtworkType.CLEARLOGO: "clearlogo",
    ArtworkType.BANNER: "banner",
    ArtworkType.LANDSCAPE: "landscape",
    ArtworkType.CLEARART: "clearart",
    ArtworkType.KEYART: "keyart",
    ArtworkType.SEASON_POSTER: "poster",
    ArtworkType.EPISODE_STILL: "",
}
ASPECT_TO_TYPE = {
    "poster": ArtworkType.POSTER,
    "clearlogo": ArtworkType.LOGO,
    "logo": ArtworkType.LOGO,
    "banner": ArtworkType.BANNER,
    "landscape": ArtworkType.LANDSCAPE,
    "thumb": ArtworkType.LANDSCAPE,
    "clearart": ArtworkType.CLEARART,
    "keyart": ArtworkType.KEYART,
}
