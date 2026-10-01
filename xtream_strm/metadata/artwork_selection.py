"""Artwork candidate selection (owned by the core, identical for every plugin).

Within one plugin the best candidate per artwork slot is chosen deterministically:

1. language: the plugin's configured metadata language first, then language-neutral
   artwork (for backdrops, landscapes and episode stills: neutral first, because text-free
   images suit them better), then anything else;
2. the source's rating, then its vote count;
3. resolution (pixel count);
4. the order in which the plugin returned the candidates, then the URL.

Across plugins the normal scraper priority/overwrite rules apply: the highest-priority
plugin that supplies a slot wins; a lower-priority plugin replaces it only when its
overwrite switch is on. A plugin never erases a selection by supplying nothing.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .models import Artwork, ArtworkType

Slot = tuple[ArtworkType, Optional[int]]

#: Types for which text-free (language-neutral) artwork is preferred.
NEUTRAL_FIRST = frozenset({ArtworkType.FANART, ArtworkType.LANDSCAPE, ArtworkType.KEYART, ArtworkType.EPISODE_STILL})
_NEUTRAL_CODES = {"", "xx", "zxx", "null", "none", "und"}


@dataclass
class PluginArtwork:
    """Artwork candidates one plugin returned for one item, with its priority settings."""

    plugin_id: str
    overwrite: bool = False
    candidates: list[Artwork] = field(default_factory=list)
    language: Optional[str] = None  # the plugin's configured metadata language, e.g. "en-US"
    remote_id: Optional[str] = None


def language_code(value: Optional[str]) -> Optional[str]:
    """``"en-US"`` -> ``"en"``; blanks and "no language" markers -> ``None`` (neutral)."""
    code = (value or "").strip().lower().replace("_", "-").split("-")[0]
    return None if code in _NEUTRAL_CODES else code


def slot_of(art: Artwork) -> Slot:
    art_type = ArtworkType(art.type)
    return art_type, (art.season_number if art_type is ArtworkType.SEASON_POSTER else None)


def _rank(art: Artwork, index: int, preferred: Optional[str]) -> tuple:
    code = language_code(art.language)
    if ArtworkType(art.type) in NEUTRAL_FIRST:
        group = 0 if code is None else 1 if code == preferred else 2
    else:
        group = 0 if code == preferred else 1 if code is None else 2
    pixels = art.width * art.height if art.width and art.height else 0
    return (group, -(art.rating or 0.0), -(art.vote_count or 0), -pixels, index, (art.url or "").strip())


def best_per_slot(candidates: Iterable[Artwork], language: Optional[str] = None) -> dict[Slot, Artwork]:
    """The best candidate per slot from one plugin.

    Listings of the same URL (compared case-insensitively, ignoring a trailing slash)
    count once, and the best-ranked listing of a URL is the one that competes, so the
    result never depends on the order of duplicates.
    """
    preferred = language_code(language) or "en"
    best: dict[Slot, tuple[tuple, Artwork]] = {}
    for index, art in enumerate(candidates or []):
        url = (art.url or "").strip()
        if not url or ArtworkType(art.type) is ArtworkType.PERSON_IMAGE:
            continue
        slot = slot_of(art)
        rank = _rank(art, index, preferred)
        if slot not in best or rank < best[slot][0]:
            best[slot] = (rank, art)
    return {slot: art for slot, (_, art) in best.items()}


def select_artwork(
    plugins: list[PluginArtwork],
    stored: Optional[dict[Slot, Artwork]] = None,
    reported: Optional[set[str]] = None,
) -> dict[Slot, Artwork]:
    """Winning candidate per slot, with provenance (``source_plugin`` ...) filled in.

    ``plugins`` are in priority order. ``stored`` is the selection persisted by an earlier
    run and ``reported`` the plugins that answered this time: a stored selection of a
    higher-priority plugin that could not answer now (e.g. an API error) is kept rather
    than silently replaced by a lower-priority plugin without overwrite.
    """
    selected: dict[Slot, Artwork] = {}
    order = {p.plugin_id: i for i, p in enumerate(plugins)}
    overwrite = {p.plugin_id: p.overwrite for p in plugins}
    for priority, plugin in enumerate(plugins, start=1):
        for slot, art in best_per_slot(plugin.candidates, plugin.language).items():
            if slot in selected and not plugin.overwrite:
                continue
            chosen = copy.deepcopy(art)
            chosen.url = chosen.url.strip()
            chosen.source_plugin = plugin.plugin_id
            chosen.source_remote_id = plugin.remote_id
            chosen.provider_priority = priority
            selected[slot] = chosen
    reported = reported or set()
    for slot, old in (stored or {}).items():
        source = old.source_plugin
        if source not in order or source in reported:
            continue
        new = selected.get(slot)
        if new is not None and order[new.source_plugin] > order[source] and not overwrite[new.source_plugin]:
            selected[slot] = old
    return selected
