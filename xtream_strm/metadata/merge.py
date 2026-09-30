"""Metadata merge rules (owned by the core, identical for every plugin).

* Overwrite OFF: a non-empty destination value is kept; empty destination values are
  filled. Collections gain the plugin's unique values after the existing ones.
* Overwrite ON: meaningful plugin values replace destination values; collections are
  replaced when the plugin supplies a non-empty collection (artwork per artwork type).
* In both modes a missing, empty or invalid plugin value never erases anything.
* External IDs and ratings are keyed by source: a plugin only ever adds or (with
  overwrite) updates its own keys; other sources' IDs/ratings are untouched.
"""

from __future__ import annotations

import copy
import unicodedata
from dataclasses import fields
from typing import Any, Callable, Iterable, Optional

from .models import (
    LIST_FIELDS,
    SCALAR_FIELDS,
    Artwork,
    Collection,
    MetadataResult,
    PersonCredit,
    Rating,
)


def _norm(text: Optional[str]) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def meaningful(value: Any) -> bool:
    """Whether ``value`` carries real information (``None``, blanks, zeros and empties don't)."""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, (list, tuple, set)):
        return any(meaningful(v) for v in value)
    if isinstance(value, dict):
        return any(meaningful(v) for v in value.values())
    if isinstance(value, Collection):
        return meaningful(value.name)
    if isinstance(value, Rating):
        return value.value is not None and value.value > 0
    if isinstance(value, PersonCredit):
        return meaningful(value.name)
    if isinstance(value, Artwork):
        return meaningful(value.url)
    return True


# -- identity rules for deduplication ------------------------------------------------------------
def _text_keys(value: str) -> list[Any]:
    return [_norm(value)]


def _actor_keys(person: PersonCredit) -> list[Any]:
    keys: list[Any] = [("name+role", _norm(person.name), _norm(person.role))]
    if person.external_id:
        keys.insert(0, ("id", person.external_id.strip().lower()))
    return keys


def _crew_keys(person: PersonCredit) -> list[Any]:
    keys: list[Any] = [("name", _norm(person.name))]
    if person.external_id:
        keys.insert(0, ("id", person.external_id.strip().lower()))
    return keys


def _artwork_keys(art: Artwork) -> list[Any]:
    url = (art.url or "").strip()
    return [(str(getattr(art.type, "value", art.type)), art.season_number, url.rstrip("/").lower())]


KEY_FUNCTIONS: dict[str, Callable[[Any], list[Any]]] = {
    "genres": _text_keys,
    "countries": _text_keys,
    "studios": _text_keys,
    "networks": _text_keys,
    "tags": _text_keys,
    "actors": _actor_keys,
    "directors": _crew_keys,
    "writers": _crew_keys,
    "artwork": _artwork_keys,
}


def dedupe(items: Iterable[Any], key_fn: Callable[[Any], list[Any]], seen: Optional[set] = None) -> list[Any]:
    """Keep the first of each identity; an item is a duplicate if *any* of its keys was seen."""
    seen = set() if seen is None else seen
    result = []
    for item in items:
        if not meaningful(item):
            continue
        keys = key_fn(item)
        if any(k in seen for k in keys):
            continue
        seen.update(keys)
        result.append(item)
    return result


# -- merge -----------------------------------------------------------------------------------------
def merge_into(dest: MetadataResult, src: MetadataResult, overwrite: bool) -> set[str]:
    """Merge ``src`` (one plugin's result) into ``dest`` in place. Returns changed field names."""
    changed: set[str] = set()

    for name in SCALAR_FIELDS + ("show_title", "season_number", "episode_number"):
        new = getattr(src, name)
        if not meaningful(new):
            continue
        current = getattr(dest, name)
        if not meaningful(current) or (overwrite and current != new and name not in ("season_number", "episode_number")):
            setattr(dest, name, copy.deepcopy(new))
            changed.add(name)

    for name in LIST_FIELDS:
        key_fn = KEY_FUNCTIONS[name]
        incoming = dedupe(getattr(src, name) or [], key_fn)
        if not incoming:
            continue
        current = list(getattr(dest, name) or [])
        if overwrite:
            if name == "artwork":
                replaced_types = {(a.type, a.season_number) for a in incoming}
                kept = [a for a in current if (a.type, a.season_number) not in replaced_types]
                merged = kept + incoming
            else:
                merged = incoming
        else:
            seen: set = set()
            merged = dedupe(current, key_fn, seen) + dedupe(incoming, key_fn, seen)
        if merged != current:
            setattr(dest, name, copy.deepcopy(merged))
            changed.add(name)

    for id_type, value in src.external_ids.items():
        key, value = (id_type or "").strip().lower(), (str(value).strip() if value is not None else "")
        if not key or not value:
            continue
        if key not in dest.external_ids or (overwrite and dest.external_ids[key] != value):
            dest.external_ids[key] = value
            changed.add("external_ids")
    if (dest.default_id_type not in dest.external_ids) and src.default_id_type in dest.external_ids:
        dest.default_id_type = src.default_id_type
        changed.add("default_id_type")

    for source, rating in src.ratings.items():
        key = (source or "").strip().lower()
        if not key or not meaningful(rating):
            continue
        if key not in dest.ratings or (overwrite and dest.ratings[key] != rating):
            dest.ratings[key] = copy.deepcopy(rating)
            changed.add("ratings")
    if (dest.default_rating not in dest.ratings) and src.default_rating in dest.ratings:
        dest.default_rating = src.default_rating
        changed.add("default_rating")

    return changed


def snapshot(result: MetadataResult) -> MetadataResult:
    return copy.deepcopy(result)


def descriptive_equal(a: MetadataResult, b: MetadataResult) -> bool:
    """Equality ignoring provenance fields (plugin_source, remote_id)."""
    skip = {"plugin_source", "remote_id"}
    return all(getattr(a, f.name) == getattr(b, f.name) for f in fields(MetadataResult) if f.name not in skip)
