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
    ) -> Iterator[LibraryItem]:
        status_clause = "AND m.status = 'active'" if active_only else ""
        provider_clause = "AND m.provider_id = ?" if provider_id is not None else ""
        params: tuple = (provider_id,) if provider_id is not None else ()
        if MOVIE in kinds:
            rows = self.db.query(
                f"SELECT m.* FROM movies m WHERE 1=1 {provider_clause} {status_clause} "
                "ORDER BY m.provider_id, m.category_id, m.title COLLATE NOCASE",
                params,
            )
            for r in rows:
                yield LibraryItem(
                    kind=MOVIE,
                    provider_id=r["provider_id"],
                    category_id=r["category_id"],
                    item_id=r["stream_id"],
                    title=r["title"],
                    year=r["year"],
                    strm_path=r["strm_path"],
                    status=r["status"],
                )
        if EPISODE in kinds:
            rows = self.db.query(
                f"""
                SELECT m.*, s.title AS series_title, s.folder_path AS series_folder
                FROM episodes m
                LEFT JOIN series s ON s.provider_id = m.provider_id AND s.category_id = m.category_id
                                   AND s.series_id = m.series_id
                WHERE 1=1 {provider_clause} {status_clause}
                ORDER BY m.provider_id, m.category_id, s.title COLLATE NOCASE, m.season, m.episode
                """,
                params,
            )
            for r in rows:
                yield LibraryItem(
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
                )

    def series(self, provider_id: Optional[int] = None, active_only: bool = True) -> Iterator[LibrarySeries]:
        clauses, params = [], []
        if provider_id is not None:
            clauses.append("provider_id = ?")
            params.append(provider_id)
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
