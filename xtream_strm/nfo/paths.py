"""The single source of truth for NFO file names.

* Movie:   ``<movie folder>/movie.nfo``
* TV show: ``<series folder>/tvshow.nfo``
* Season:  ``<season folder>/season.nfo``
* Episode: same basename as the episode ``.strm``, in the same folder

Metadata plugins and the probe engine must use these helpers; nothing else may invent
NFO file names.
"""

from __future__ import annotations

import enum
import os


class NfoKind(str, enum.Enum):
    MOVIE = "movie"
    TVSHOW = "tvshow"
    SEASON = "season"
    EPISODE = "episode"

    @property
    def root_tag(self) -> str:
        return ROOT_TAGS[self]


ROOT_TAGS = {
    NfoKind.MOVIE: "movie",
    NfoKind.TVSHOW: "tvshow",
    NfoKind.SEASON: "season",
    NfoKind.EPISODE: "episodedetails",
}

MOVIE_NFO = "movie.nfo"
TVSHOW_NFO = "tvshow.nfo"
SEASON_NFO = "season.nfo"
NFO_EXTENSION = ".nfo"


def movie_nfo_path(movie_folder: str) -> str:
    return os.path.join(movie_folder, MOVIE_NFO)


def tvshow_nfo_path(series_folder: str) -> str:
    return os.path.join(series_folder, TVSHOW_NFO)


def season_nfo_path(season_folder: str) -> str:
    return os.path.join(season_folder, SEASON_NFO)


def episode_nfo_path(episode_strm_path: str) -> str:
    stem, _ = os.path.splitext(episode_strm_path)
    return stem + NFO_EXTENSION


def nfo_path_for(kind: NfoKind, path: str) -> str:
    """``path`` is the movie/series/season folder, or the episode's ``.strm`` file."""
    if kind is NfoKind.MOVIE:
        return movie_nfo_path(path)
    if kind is NfoKind.TVSHOW:
        return tvshow_nfo_path(path)
    if kind is NfoKind.SEASON:
        return season_nfo_path(path)
    return episode_nfo_path(path)
