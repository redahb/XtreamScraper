"""Conversion of Canal+ search records into shared match candidates and the normalized model.

Only what the records reliably carry is mapped. Left unset on purpose (another plugin may fill
them through the core merge): series year (``show`` records have none), runtime, release and air
dates (``tvcpCreatedDate`` is the catalog-add date), episode year (``tvcpYear`` of an episode is
its season's), producers, studios, networks, countries, ratings and votes, trailers, season
titles and plots (only ``Seizoen N``), the generic SEO ``description``, and season artwork.

Artwork: ``image`` (portrait) is the poster, ``image_landscape`` the fanart and an episode's own
``image_landscape`` its still. Addresses are only taken from Canal+'s image storage
(:func:`~.parsing.asset`). A watermarked ``/marked/`` image is replaced by its clean ``/ori/``
counterpart only for the providers in :data:`CLEAN_VARIANT_PROVIDERS`, whose counterparts were
checked to exist; other providers keep their working ``/marked/`` address.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from xtreamscraper.core.matching import MatchCandidate
from xtreamscraper.metadata.models import Artwork, ArtworkType, MediaType, MetadataResult, PersonCredit
from xtreamscraper.utils.coerce import as_text

from .parsing import MOVIE, Asset, Person, SeriesFamily, asset, merge_people, people, season_episode, year_of
from .sites import NL, Site

NAMESPACE = "canalplus"
MARKED, CLEAN = "marked", "ori"
#: Providers whose ``/marked/`` images were verified to exist under ``/ori/`` without the
#: watermark (posters, fanart and episode stills): ``cpnl`` and ``viaplaynl`` (NL) on 2026-10-09,
#: ``lovnaten`` (AT, HU), ``appletvcz`` and ``cpcz`` (CZ, SK) and ``histint`` (HU) on 2026-10-15, every
#: image of each. Not ``dohu`` (HU): 26 of its 754 images have no clean version. Extend only with evidence.
CLEAN_VARIANT_PROVIDERS = frozenset({"cpnl", "viaplaynl", "lovnaten", "appletvcz", "cpcz", "histint"})
DIRECTOR_ROLE = "director"


# -- candidates -----------------------------------------------------------------------------------
def candidates(hits: Iterable[dict[str, Any]], kind: str) -> list[MatchCandidate]:
    """One candidate per record; only a movie carries a year (series ``show`` records have none)."""
    return [MatchCandidate(remote_id=hit["objectID"], title=hit["title"].strip(),
                           year=year_of(hit.get("tvcpYear")) if kind == MOVIE else None)
            for hit in hits]


# -- fields ---------------------------------------------------------------------------------------
def plot(value: Any) -> Optional[str]:
    """The plot with its line and paragraph breaks kept (``\\r\\n`` -> ``\\n``), outer space trimmed."""
    if not isinstance(value, str):
        return None
    return value.replace("\r\n", "\n").replace("\r", "\n").strip() or None


def genres(record: dict[str, Any]) -> Optional[list[str]]:
    """``genres`` (a list), else ``tvcpGenres`` split on commas; de-duplicated case-insensitively."""
    raw = record.get("genres")
    values = raw if isinstance(raw, list) and any(as_text(v) for v in raw) else \
        (record.get("tvcpGenres").split(",") if isinstance(record.get("tvcpGenres"), str) else [])
    found: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(value.split()) if isinstance(value, str) else ""
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            found.append(text)
    return found or None


def certification(value: Any) -> Optional[str]:
    """``tvcpRating`` exactly as supplied (``"12+"``), only trimmed."""
    return as_text(value)


def actors_of(record: dict[str, Any]) -> list[Person]:
    return people(record.get("tvcpActors"))


def directors_of(record: dict[str, Any]) -> list[Person]:
    """``tvcpDirectors`` entries whose role is exactly ``director`` (producers are left out)."""
    return [person for person in people(record.get("tvcpDirectors")) if person.role == DIRECTOR_ROLE]


def _actor_credits(found: list[Person]) -> Optional[list[PersonCredit]]:
    return [PersonCredit(person.name, order=index) for index, person in enumerate(found)] or None


def _director_credits(found: list[Person]) -> Optional[list[PersonCredit]]:
    return [PersonCredit(person.name, job="Director", department="Directing") for person in found] or None


# -- artwork --------------------------------------------------------------------------------------
def clean_asset(url: Any, vod_provider: Any = None) -> Optional[Asset]:
    """The image to emit for a record's address, or ``None``.

    ``/ori/`` is kept; ``/marked/`` becomes ``/ori/`` only for a provider in
    :data:`CLEAN_VARIANT_PROVIDERS` (and only when a record's ``vodProvider``, if given, names the
    same provider); other ``/marked/`` images are kept as they are. Any other variant (such as
    ``landscape_small``) or address is not emitted. The host and bucket never change.
    """
    found = asset(url)
    if found is None or found.variant not in (MARKED, CLEAN):
        return None
    provider_matches = not isinstance(vod_provider, str) or vod_provider.strip() == found.provider
    if found.variant == MARKED and found.provider in CLEAN_VARIANT_PROVIDERS and provider_matches:
        return found._replace(variant=CLEAN)
    return found


def _artwork(kind: ArtworkType, url: Any, record: dict[str, Any], orientation: str) -> Optional[Artwork]:
    found = clean_asset(url, record.get("vodProvider"))
    if found is None or found.orientation != orientation:
        return None
    return Artwork(kind, found.url, source=NAMESPACE)


def show_artwork(record: dict[str, Any]) -> Optional[list[Artwork]]:
    """Poster from ``image`` (portrait) and fanart from ``image_landscape`` (landscape)."""
    found = [art for art in (_artwork(ArtworkType.POSTER, record.get("image"), record, "portrait"),
                             _artwork(ArtworkType.FANART, record.get("image_landscape"), record, "landscape"))
             if art is not None]
    return found or None


def episode_still(episode: dict[str, Any], show: Optional[dict[str, Any]]) -> Optional[Artwork]:
    """The episode's own landscape image; skipped when it is the show's fanart image again."""
    still = _artwork(ArtworkType.EPISODE_STILL, episode.get("image_landscape"), episode, "landscape")
    if still is None:
        return None
    own = asset(episode.get("image_landscape"))
    shown = asset(show.get("image_landscape")) if isinstance(show, dict) else None
    if own is not None and shown is not None and own.key == shown.key:
        return None
    return still


# -- results --------------------------------------------------------------------------------------
def _ids(object_id: str) -> dict[str, str]:
    return {NAMESPACE: object_id}


def map_movie(record: dict[str, Any]) -> MetadataResult:
    object_id = record["objectID"]
    return MetadataResult(
        MediaType.MOVIE,
        title=as_text(record.get("title")),
        plot=plot(record.get("content")),
        year=year_of(record.get("tvcpYear")),
        genres=genres(record),
        certification=certification(record.get("tvcpRating")),
        actors=_actor_credits(actors_of(record)),
        directors=_director_credits(directors_of(record)),
        external_ids=_ids(object_id),
        default_id_type=NAMESPACE,
        artwork=show_artwork(record),
        plugin_source=NAMESPACE,
        remote_id=object_id,
    )


def series_people(record: dict[str, Any], family: SeriesFamily) -> tuple[list[Person], list[Person]]:
    """Actors and directors of a series: the show record's, then per season in number order the
    season record's, or (when that season record has none, or there is no season record) its
    episodes'. De-duplicated case-insensitively, first spelling and order kept."""
    actors, directors = [actors_of(record)], [directors_of(record)]
    for number in sorted(family.seasons):
        season = family.seasons[number]
        season_actors = actors_of(season) if season else []
        season_directors = directors_of(season) if season else []
        if season_actors or season_directors:
            actors.append(season_actors)
            directors.append(season_directors)
            continue
        for episode in family.season_episodes(number):
            actors.append(actors_of(episode))
            directors.append(directors_of(episode))
    return merge_people(*actors), merge_people(*directors)


def map_series(record: dict[str, Any], family: SeriesFamily) -> MetadataResult:
    """A series; its year is never taken from a season."""
    object_id = record["objectID"]
    actors, directors = series_people(record, family)
    return MetadataResult(
        MediaType.SERIES,
        title=as_text(record.get("title")),
        plot=plot(record.get("content")),
        genres=genres(record),
        certification=certification(record.get("tvcpRating")),
        actors=_actor_credits(actors),
        directors=_director_credits(directors),
        external_ids=_ids(object_id),
        default_id_type=NAMESPACE,
        artwork=show_artwork(record),
        plugin_source=NAMESPACE,
        remote_id=object_id,
    )


def map_season(season_number: int, record: Optional[dict[str, Any]]) -> MetadataResult:
    """The number and, from a real season record, its year. Canal+'s ``Seizoen N`` title, its
    empty plot and its image (not a distinct season poster) are not mapped."""
    return MetadataResult(
        MediaType.SEASON,
        season_number=season_number,
        year=year_of(record.get("tvcpYear")) if record else None,
        plugin_source=NAMESPACE,
        remote_id=record.get("objectID") if record else None,
    )


def map_episode(hit: dict[str, Any], show: Optional[dict[str, Any]], site: Site = NL) -> MetadataResult:
    """An episode; no year or air date (``tvcpYear`` is the season's) and no invented title."""
    object_id = hit["objectID"]
    season, episode = season_episode(hit, site)
    still = episode_still(hit, show)
    return MetadataResult(
        MediaType.EPISODE,
        title=as_text(hit.get("tvcpEpisodeTitle")),
        plot=plot(hit.get("content")),
        season_number=season,
        episode_number=episode,
        external_ids=_ids(object_id),
        artwork=[still] if still else None,
        plugin_source=NAMESPACE,
        remote_id=object_id,
    )
