"""TMDB JSON -> normalized :class:`MetadataResult`.

Only data the core model represents is mapped; everything is defensive against missing,
null or oddly typed fields.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Optional

from xtreamscraper.metadata.models import (
    Artwork,
    ArtworkType,
    Collection,
    MediaType,
    MetadataResult,
    PersonCredit,
    Rating,
)
from xtreamscraper.utils.coerce import as_dict as _d
from xtreamscraper.utils.coerce import as_int
from xtreamscraper.utils.coerce import as_list
from xtreamscraper.utils.coerce import dict_items as _l
from xtreamscraper.utils.coerce import scalar_text as _s

_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
WRITER_JOBS = ("Writer", "Screenplay", "Story", "Teleplay")
MAX_ACTORS = 50
# Our external-ID namespace -> TMDB external_ids key.
EXTERNAL_ID_KEYS = {"imdb": "imdb_id", "tvdb": "tvdb_id", "wikidata": "wikidata_id"}


def _num(value: Any) -> Optional[float]:
    """A positive, finite number from a number or numeric text; ``None`` for anything else.

    Contract:
        Silences:
            - ``TypeError``/``ValueError``/``OverflowError`` from ``float()`` (non-numbers, ints
              too large for a float): ``None``. Zero, negatives, NaN and infinities are ``None``
              too, so "missing" and "zero" look the same. ``True`` reads as 1.0.
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def valid_date(value: Any) -> Optional[str]:
    """``value`` as text when it looks like ``YYYY-MM-DD`` (year 1800-2200, month 1-12), else ``None``.

    Contract:
        Silences:
            - The day is only checked for two digits: ``2020-02-31`` and ``2020-01-00`` pass and are
              returned as they are.
    """
    text = _s(value)
    if not text:
        return None
    match = _DATE.match(text)
    if not match or not (1800 <= int(match.group(1)) <= 2200) or not (1 <= int(match.group(2)) <= 12):
        return None
    return text


def year_of(value: Any) -> Optional[int]:
    date = valid_date(value)
    return int(date[:4]) if date else None


class ImageUrls:
    """Builds image URLs from TMDB's /configuration (no hard-coded base URL)."""

    base: str
    profile_size: str

    def __init__(self, configuration: dict[str, Any]) -> None:
        images = _d(configuration.get("images"))
        self.base = (_s(images.get("secure_base_url")) or _s(images.get("base_url")) or "").rstrip("/")
        self.profile_size = "h632" if "h632" in as_list(images.get("profile_sizes")) else "original"

    def url(self, path: Any, size: str = "original") -> Optional[str]:
        """``<base>/<size>/<path>``; ``None`` without a path or without a base URL from the configuration."""
        path = _s(path)
        if not path or not self.base:
            return None
        return f"{self.base}/{size}/{path.lstrip('/')}"

    def profile(self, path: Any) -> Optional[str]:
        return self.url(path, self.profile_size)


def _lang(language: str) -> str:
    return (language or "en").split("-")[0].lower()


def _pick_images(items: list[dict[str, Any]], language: str, prefer_neutral: bool = False) -> list[dict[str, Any]]:
    """Configured language first, then language-neutral (or the reverse for backdrops).

    Within a group, a higher vote average and then a higher vote count come first; the sort is
    stable, so TMDB's order breaks remaining ties. Images without a ``file_path`` are dropped.
    """
    lang = _lang(language)

    def rank(image: dict[str, Any]) -> tuple[int, float, float]:
        code = image.get("iso_639_1")
        group = (0 if code is None else 1 if code == lang else 2) if prefer_neutral \
            else (0 if code == lang else 1 if code is None else 2)
        return (group, -(_num(image.get("vote_average")) or 0), -(_num(image.get("vote_count")) or 0))

    return sorted((i for i in items if _s(i.get("file_path"))), key=rank)


MAX_ART_CANDIDATES = 8


def _artwork(details: dict[str, Any], urls: ImageUrls, language: str, still: bool = False) -> list[Artwork]:
    """Normalized artwork candidates (several per type); the core selects and stores the winner.

    Up to ``MAX_ART_CANDIDATES`` per type, in ``_pick_images`` order. Posters and fanart fall back
    to ``poster_path``/``backdrop_path`` (episodes: ``still_path``) only when no image of the
    type gave a URL. Logos are limited to the configured language and language-neutral ones.

    Contract:
        Silences:
            - Without an image base URL (``urls.base`` empty) every URL is ``None`` and the list
              is empty, with no error.
    """
    images = _d(details.get("images"))
    art: list[Artwork] = []

    def add(kind: ArtworkType, candidates: list[dict[str, Any]], fallback_path: Any = None) -> None:
        added = 0
        for image in candidates[:MAX_ART_CANDIDATES]:
            url = urls.url(image.get("file_path"))
            if not url:
                continue
            art.append(Artwork(kind, url, language=image.get("iso_639_1"),
                               width=image["width"] if isinstance(image.get("width"), int) else None,
                               height=image["height"] if isinstance(image.get("height"), int) else None,
                               rating=_num(image.get("vote_average")),
                               vote_count=int(_num(image.get("vote_count")) or 0) or None, source="tmdb"))
            added += 1
        if not added:
            url = urls.url(fallback_path)
            if url:
                art.append(Artwork(kind, url, source="tmdb"))

    if still:
        add(ArtworkType.EPISODE_STILL, _pick_images(_l(images.get("stills")), language, prefer_neutral=True),
            details.get("still_path"))
        return art
    add(ArtworkType.POSTER, _pick_images(_l(images.get("posters")), language), details.get("poster_path"))
    add(ArtworkType.FANART, _pick_images(_l(images.get("backdrops")), language, prefer_neutral=True), details.get("backdrop_path"))
    logos = [i for i in _pick_images(_l(images.get("logos")), language) if i.get("iso_639_1") in (_lang(language), None)]
    add(ArtworkType.CLEARLOGO, logos)
    return art


def _unique(values: Iterable[Optional[str]]) -> Optional[list[str]]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            result.append(value)
    return result or None


def _genres(details: dict[str, Any]) -> Optional[list[str]]:
    return _unique(_s(x.get("name")) for x in _l(details.get("genres")))


def _names(items: Any) -> Optional[list[str]]:
    return _unique(_s(x.get("name")) for x in _l(items))


def _countries(details: dict[str, Any]) -> Optional[list[str]]:
    """Production country names, then ``origin_country`` codes (as names when a production country has the code).

    Contract:
        Silences:
            - An origin code that is no production country is kept as the upper-case code, so the
              list can mix names and ISO codes.
    """
    by_code = {(_s(c.get("iso_3166_1")) or "").upper(): _s(c.get("name")) for c in _l(details.get("production_countries"))}
    names = [n for n in by_code.values() if n]
    for code in as_list(details.get("origin_country")):
        if isinstance(code, str) and code.strip():
            names.append(by_code.get(code.strip().upper()) or code.strip().upper())
    return _unique(names)


def _cast(people: Iterable[dict[str, Any]], urls: ImageUrls, aggregate: bool = False) -> Optional[list[PersonCredit]]:
    """Actors sorted by TMDB ``order`` (list position when it is missing), at most ``MAX_ACTORS``.

    Contract:
        Silences:
            - People without a name are skipped. With ``aggregate`` the first role's character is
              used when ``character`` is empty.
            - Not deduplicated: when ``people`` joins two lists (episode cast + guest stars), their
              ``order`` values are interleaved and a person listed in both appears twice.
    """
    result: list[PersonCredit] = []
    for index, person in enumerate(people):
        name = _s(person.get("name"))
        if not name:
            continue
        role = _s(person.get("character"))
        if aggregate and not role:
            roles = _l(person.get("roles"))
            role = _s(roles[0].get("character")) if roles else None
        order = person.get("order")
        result.append(PersonCredit(
            name=name, role=role, order=order if isinstance(order, int) else index,
            external_id=f"tmdb:{person['id']}" if person.get("id") is not None else None,
            profile_image=urls.profile(person.get("profile_path")), department="Acting",
        ))
    result.sort(key=lambda p: p.order if p.order is not None else 10**6)
    return result[:MAX_ACTORS] or None


def _crew(crew: Iterable[dict[str, Any]], jobs: Iterable[str]) -> Optional[list[PersonCredit]]:
    """Crew members with one of ``jobs``, each person once (by TMDB ID, else by name), first job kept."""
    wanted = set(jobs)
    seen: set[tuple[str, str]] = set()
    result: list[PersonCredit] = []
    for person in crew:
        name, job = _s(person.get("name")), _s(person.get("job"))
        if not name or job not in wanted:
            continue
        person_id = _s(person.get("id"))
        key = ("id", person_id) if person_id else ("name", name.casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append(PersonCredit(name=name, job=job, department=_s(person.get("department")),
                                   external_id=f"tmdb:{person_id}" if person_id else None))
    return result or None


def _external_ids(details: dict[str, Any]) -> dict[str, str]:
    """IMDb/TVDB/Wikidata IDs from ``external_ids`` (a top-level ``imdb_id`` wins); ``"0"`` is no ID."""
    source = {**_d(details.get("external_ids")), **{k: details.get(k) for k in ("imdb_id",) if details.get(k)}}
    result: dict[str, str] = {}
    for namespace, key in EXTERNAL_ID_KEYS.items():
        value = _s(source.get(key))
        if value and value not in ("0",):
            result[namespace] = value
    return result


def _rating(details: dict[str, Any]) -> dict[str, Rating]:
    value = _num(details.get("vote_average"))
    votes = details.get("vote_count")
    if value is None:
        return {}
    return {"tmdb": Rating(round(value, 3), int(votes) if isinstance(votes, int) and votes > 0 else None, 10.0)}


def pick_trailer(details: dict[str, Any], language: str) -> Optional[str]:
    """Conservative trailer choice: YouTube trailers, official first, configured language first.

    Then larger ``size`` first, then the oldest ``published_at`` (undated last).

    Contract:
        Silences:
            - Videos of another type or site, or without a key, are ignored; a numeric key is
              used as text. ``None`` when no trailer is left.
    """
    lang = _lang(language)
    trailers = [v for v in _l(_d(details.get("videos")).get("results"))
                if v.get("type") == "Trailer" and v.get("site") == "YouTube" and _s(v.get("key"))]
    if not trailers:
        return None
    trailers.sort(key=lambda v: (not v.get("official", False), v.get("iso_639_1") != lang,
                                 -(_num(v.get("size")) or 0), _s(v.get("published_at")) or "9999"))
    return f"https://www.youtube.com/watch?v={_s(trailers[0].get('key'))}"


def _region(region: str, language: str) -> Optional[str]:
    if region:
        return region.upper()
    parts = (language or "").split("-")
    return parts[1].upper() if len(parts) == 2 and len(parts[1]) == 2 else None


def movie_certification(details: dict[str, Any], region: str, language: str) -> Optional[str]:
    """The certification for ``region`` (else the language's country, e.g. ``nl-BE`` -> BE).

    Release types are tried theatrical, limited, digital, physical, TV, premiere, then any other;
    the first non-empty certification wins. ``None`` without a country or a certification.
    """
    country = _region(region, language)
    if not country:
        return None
    for entry in _l(_d(details.get("release_dates")).get("results")):
        if (_s(entry.get("iso_3166_1")) or "").upper() != country:
            continue
        # theatrical (3) first, then limited (2), digital (4), physical (5), TV (6), premiere (1)
        order = {3: 0, 2: 1, 4: 2, 5: 3, 6: 4, 1: 5}
        dates = sorted(_l(entry.get("release_dates")), key=lambda d: order.get(as_int(d.get("type")), 9))
        for release in dates:
            cert = _s(release.get("certification"))
            if cert:
                return cert
    return None


def tv_certification(details: dict[str, Any], region: str, language: str) -> Optional[str]:
    """The content rating of the first entry for the country (see ``movie_certification``).

    ``None`` when that entry's rating is empty; later entries for the same country are not tried.
    """
    country = _region(region, language)
    if not country:
        return None
    for entry in _l(_d(details.get("content_ratings")).get("results")):
        if (_s(entry.get("iso_3166_1")) or "").upper() == country:
            return _s(entry.get("rating"))
    return None


def _keywords(details: dict[str, Any]) -> Optional[list[str]]:
    data = _d(details.get("keywords"))
    return _names(data.get("keywords") or data.get("results"))


def map_movie(details: dict[str, Any], urls: ImageUrls, language: str, region: str) -> MetadataResult:
    """A movie result from TMDB movie details (``map_tv``/``map_season``/``map_episode`` alike).

    Contract:
        Preconditions:
            - ``details`` is a dict (``TMDBClient`` guarantees a JSON object). Any other type
              raises ``AttributeError`` (implicit).

        Silences:
            - Missing, null or oddly typed fields become ``None``/empty (see ``utils/coerce``);
              the result can be nearly empty without any error. ``details["id"]`` of any type
              is used as text for the TMDB ID.
    """
    credits = _d(details.get("credits"))
    collection = _d(details.get("belongs_to_collection"))
    runtime = details.get("runtime")
    ids = {"tmdb": str(details["id"])} if details.get("id") is not None else {}
    ids.update(_external_ids(details))
    return MetadataResult(
        media_type=MediaType.MOVIE,
        title=_s(details.get("title")),
        original_title=_s(details.get("original_title")),
        plot=_s(details.get("overview")),
        tagline=_s(details.get("tagline")),
        premiered=valid_date(details.get("release_date")),
        year=year_of(details.get("release_date")),
        runtime_minutes=runtime if isinstance(runtime, int) and runtime > 0 else None,
        genres=_genres(details),
        countries=_countries(details),
        studios=_names(details.get("production_companies")),
        tags=_keywords(details),
        collection=Collection(name=_s(collection.get("name")), external_id=str(collection["id"]) if collection.get("id") else None)
        if _s(collection.get("name")) else None,
        ratings=_rating(details),
        certification=movie_certification(details, region, language),
        directors=_crew(_l(credits.get("crew")), ("Director",)),
        writers=_crew(_l(credits.get("crew")), WRITER_JOBS),
        actors=_cast(_l(credits.get("cast")), urls),
        external_ids=ids,
        default_id_type="tmdb" if ids.get("tmdb") else None,
        default_rating="tmdb" if _rating(details) else None,
        artwork=_artwork(details, urls, language) or None,
        trailer=pick_trailer(details, language),
        plugin_source="tmdb",
        remote_id=ids.get("tmdb"),
    )


def map_tv(details: dict[str, Any], urls: ImageUrls, language: str, region: str) -> MetadataResult:
    ids = {"tmdb": str(details["id"])} if details.get("id") is not None else {}
    ids.update(_external_ids(details))
    creators = [PersonCredit(name=n, job="Creator", external_id=f"tmdb:{c['id']}" if c.get("id") is not None else None)
                for c in _l(details.get("created_by")) for n in [_s(c.get("name"))] if n] or None
    credits = _d(details.get("aggregate_credits")) or _d(details.get("credits"))
    art = _artwork(details, urls, language)
    for season in _l(details.get("seasons")):
        number, url = season.get("season_number"), urls.url(season.get("poster_path"))
        if isinstance(number, int) and number >= 0 and url:
            art.append(Artwork(ArtworkType.SEASON_POSTER, url, season_number=number, source="tmdb"))
    return MetadataResult(
        media_type=MediaType.SERIES,
        title=_s(details.get("name")),
        original_title=_s(details.get("original_name")),
        plot=_s(details.get("overview")),
        tagline=_s(details.get("tagline")),
        premiered=valid_date(details.get("first_air_date")),
        year=year_of(details.get("first_air_date")),
        status=_s(details.get("status")),
        genres=_genres(details),
        countries=_countries(details),
        studios=_names(details.get("production_companies")),
        networks=_names(details.get("networks")),
        tags=_keywords(details),
        ratings=_rating(details),
        certification=tv_certification(details, region, language),
        writers=creators,
        actors=_cast(_l(credits.get("cast")), urls, aggregate=True),
        external_ids=ids,
        default_id_type="tmdb" if ids.get("tmdb") else None,
        default_rating="tmdb" if _rating(details) else None,
        artwork=art or None,
        trailer=pick_trailer(details, language),
        plugin_source="tmdb",
        remote_id=ids.get("tmdb"),
    )


def map_season(details: dict[str, Any], urls: ImageUrls, language: str) -> MetadataResult:
    number = details.get("season_number")
    ids = {"tmdb": str(details["id"])} if details.get("id") is not None else {}
    tvdb = _s(_d(details.get("external_ids")).get("tvdb_id"))
    if tvdb:
        ids["tvdb"] = tvdb
    poster = urls.url((_pick_images(_l(_d(details.get("images")).get("posters")), language) or [{}])[0].get("file_path")
                      or details.get("poster_path"))
    return MetadataResult(
        media_type=MediaType.SEASON,
        title=_s(details.get("name")),
        plot=_s(details.get("overview")),
        premiered=valid_date(details.get("air_date")),
        year=year_of(details.get("air_date")),
        season_number=number if isinstance(number, int) and number >= 0 else None,
        ratings=_rating(details),
        external_ids=ids,
        artwork=[Artwork(ArtworkType.POSTER, poster, source="tmdb")] if poster else None,
        plugin_source="tmdb",
        remote_id=ids.get("tmdb"),
    )


def map_episode(details: dict[str, Any], urls: ImageUrls, language: str) -> MetadataResult:
    ids = {"tmdb": str(details["id"])} if details.get("id") is not None else {}
    ids.update(_external_ids(details))
    credits = _d(details.get("credits"))
    crew = _l(details.get("crew")) or _l(credits.get("crew"))
    guests = _l(details.get("guest_stars")) or _l(credits.get("guest_stars"))
    runtime = details.get("runtime")
    return MetadataResult(
        media_type=MediaType.EPISODE,
        title=_s(details.get("name")),
        plot=_s(details.get("overview")),
        premiered=valid_date(details.get("air_date")),
        runtime_minutes=runtime if isinstance(runtime, int) and runtime > 0 else None,
        production_code=_s(details.get("production_code")),
        ratings=_rating(details),
        directors=_crew(crew, ("Director",)),
        writers=_crew(crew, WRITER_JOBS),
        actors=_cast([*_l(credits.get("cast")), *guests], urls),
        external_ids=ids,
        artwork=_artwork(details, urls, language, still=True) or None,
        plugin_source="tmdb",
        remote_id=ids.get("tmdb"),
    )
