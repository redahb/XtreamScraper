"""Read-only view of the generated library, built from synchronization state.

Probing and metadata enrichment use this instead of talking to providers, so both can
run on an existing library without another Xtream synchronization.
"""

from __future__ import annotations

from typing import Iterator, Optional, Sequence

from ..storage.db import Database
from .models import EPISODE, MOVIE, LibraryItem, LibrarySeries


class LibraryIndex:
    def __init__(self, db: Database) -> None:
        self.db = db

    def items(
        self,
        provider_id: Optional[int] = None,
        kinds: Sequence[str] = (MOVIE, EPISODE),
        active_only: bool = True,
        item: Optional[tuple[str, str]] = None,
    ) -> list[LibraryItem]:
        """All playable items, movies first. Returned as a list: the rows are read in full
        anyway, and callers may run their own queries while they go through the items.

        ``item``: only one root item, ``(category_id, id)``: the movie with that stream ID
        and the episodes of the series with that series ID.

        Contract:
            Preconditions:
                - With ``item``, pass a single kind: VOD and series IDs are separate number
                  spaces, so with both kinds a movie and an unrelated series that share
                  ``(category_id, id)`` are both returned (no error).
            Raises:
                sqlite3.Error: From the queries.
        """
        items: list[LibraryItem] = []
        status_clause = "AND m.status = 'active'" if active_only else ""
        provider_clause = "AND m.provider_id = ?" if provider_id is not None else ""
        params: tuple[object, ...] = (provider_id,) if provider_id is not None else ()
        if item is not None:
            params += (str(item[0]), str(item[1]))
        if MOVIE in kinds:
            item_clause = "AND m.category_id = ? AND m.stream_id = ?" if item is not None else ""
            rows = self.db.query(
                f"SELECT m.* FROM movies m WHERE 1=1 {provider_clause} {status_clause} {item_clause} "
                "ORDER BY m.provider_id, m.category_id, m.title COLLATE NOCASE",
                params,
            )
            for r in rows:
                items.append(LibraryItem(
                    kind=MOVIE,
                    provider_id=r["provider_id"],
                    category_id=r["category_id"],
                    item_id=r["stream_id"],
                    title=r["title"],
                    year=r["year"],
                    strm_path=r["strm_path"],
                    status=r["status"],
                    url_hash=r["url_hash"],
                ))
        if EPISODE in kinds:
            item_clause = "AND m.category_id = ? AND m.series_id = ?" if item is not None else ""
            rows = self.db.query(
                f"""
                SELECT m.*, s.title AS series_title, s.folder_path AS series_folder
                FROM episodes m
                LEFT JOIN series s ON s.provider_id = m.provider_id AND s.category_id = m.category_id
                                   AND s.series_id = m.series_id
                WHERE 1=1 {provider_clause} {status_clause} {item_clause}
                ORDER BY m.provider_id, m.category_id, s.title COLLATE NOCASE, m.season, m.episode
                """,
                params,
            )
            for r in rows:
                items.append(LibraryItem(
                    kind=EPISODE,
                    provider_id=r["provider_id"],
                    category_id=r["category_id"],
                    item_id=r["episode_id"],
                    title=r["title"] or "",
                    strm_path=r["strm_path"],
                    status=r["status"],
                    series_id=r["series_id"],
                    series_title=r["series_title"],
                    series_folder=r["series_folder"],
                    season=r["season"],
                    episode=r["episode"],
                    url_hash=r["url_hash"],
                ))
        return items

    def series(self, provider_id: Optional[int] = None, active_only: bool = True,
               item: Optional[tuple[str, str]] = None) -> Iterator[LibrarySeries]:
        """Series, by title. ``item``: only the series ``(category_id, series_id)``.

        Contract:
            Raises:
                sqlite3.Error: From the query, raised on the first iteration (this is a
                    generator), not when it is called.
        """
        clauses: list[str] = []
        params: list[object] = []
        if provider_id is not None:
            clauses.append("provider_id = ?")
            params.append(provider_id)
        if item is not None:
            clauses.append("category_id = ? AND series_id = ?")
            params += [str(item[0]), str(item[1])]
        if active_only:
            clauses.append("status = 'active'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        for r in self.db.query(f"SELECT * FROM series {where} ORDER BY title COLLATE NOCASE", params):
            yield LibrarySeries(
                provider_id=r["provider_id"],
                category_id=r["category_id"],
                series_id=r["series_id"],
                title=r["title"],
                year=r["year"],
                folder_path=r["folder_path"],
                status=r["status"],
            )
