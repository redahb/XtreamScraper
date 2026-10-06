"""Persistence for scraper plugins: manager state, plugin configuration and bindings.

Enabled/overwrite/priority belong to the scraper manager (``scraper_plugins``); each
plugin's own settings are stored separately (``scraper_config``). Secrets in plugin
configuration are stored in plain text in the local database, like provider passwords.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Iterable, Optional, Sequence, TypedDict, cast

from ..utils.timeutil import now_iso
from .db import Database

# Binding / scrape-state statuses
MATCHED = "matched"
UNMATCHED = "unmatched"
AMBIGUOUS = "ambiguous"
INVALID_BINDING = "invalid_binding"
API_ERROR = "api_error"
NOT_FOUND = "not_found"  # parent matched, but the remote season/episode does not exist

#: Root (movie/series) binding states a user can fix by entering an ID. ``api_error`` is not one
#: of them: the source failed for a while and nothing is known about the match.
UNRESOLVED = (UNMATCHED, AMBIGUOUS, INVALID_BINDING, NOT_FOUND)
#: Filters of the root-item overview. ``matched``: at least one plugin in scope has a match;
#: ``unmatched``: none has (ambiguous, not found and errors included); the others: at least one
#: plugin in scope is in that state; ``manual``: at least one match was set by hand.
OVERVIEW_FILTERS = (UNMATCHED, AMBIGUOUS, INVALID_BINDING, NOT_FOUND, MATCHED, "manual")
#: SQL: binding ``b`` holds a match (an API error keeps the stored match).
HAS_MATCH = "(b.status = 'matched' OR (b.status = 'api_error' AND COALESCE(b.remote_id, '') != ''))"
ROOT_KINDS = ("movie", "series")

#: ``(plugin_id, item_kind, provider_id, category_id, item_id)``: :attr:`Binding.key`.
BindingKey = tuple[str, str, int, str, str]
#: ``(item_kind, provider_id, category_id, item_id)``: one item, whichever plugin (note the order
#: differs from the artwork/NFO ``ItemKey``).
BindingItemKey = tuple[str, int, str, str]


class OverviewRow(TypedDict):
    """One root item listed by :meth:`ScraperStateRepository.root_overview`."""

    kind: str  # movie | series
    provider_id: int
    category_id: str
    item_id: str
    title: str
    year: Optional[int]
    provider_name: str
    category_name: Optional[str]  # None when the category row is gone


@dataclass
class PluginState:
    plugin_id: str
    enabled: bool
    overwrite: bool
    priority: int


@dataclass
class Binding:
    plugin_id: str
    item_kind: str  # movie | series | season | episode
    provider_id: int
    category_id: str
    item_id: str
    status: str
    remote_id: Optional[str] = None
    remote_parent_id: Optional[str] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    match_method: Optional[str] = None
    match_score: Optional[float] = None
    matched_title: Optional[str] = None
    matched_year: Optional[int] = None
    matched_at: Optional[str] = None
    last_successful_scrape: Optional[str] = None
    last_attempt: Optional[str] = None
    message: Optional[str] = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    config_fingerprint: Optional[str] = None
    #: IDs the user entered by hand and confirmed for this match (``{namespace: value}``): the
    #: native remote ID plus the alternate ID it was found with. Empty for automatic matches.
    manual_ids: dict[str, str] = field(default_factory=dict)

    @property
    def manual(self) -> bool:
        return bool(self.manual_ids)

    @property
    def key(self) -> BindingKey:
        return (self.plugin_id, self.item_kind, self.provider_id, self.category_id, self.item_id)


_BINDING_KEYS = ("plugin_id", "item_kind", "provider_id", "category_id", "item_id")


def _json_dict(text: str) -> dict[str, Any]:
    """A stored JSON object; unreadable JSON or any other JSON value reads as ``{}``."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class ScraperStateRepository:
    db: Database

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- manager state -------------------------------------------------------------------------
    def ensure_installed(self, plugin_ids: Iterable[str]) -> None:
        """Give newly installed plugins a row: disabled, overwrite off, at the bottom of the list.

        Plugins that appear at the same time are appended by plugin ID, so the order never
        depends on how or when plugins were loaded. After that only the user changes it.
        """
        now = now_iso()
        with self.db.transaction() as conn:
            existing = {r["plugin_id"] for r in conn.execute("SELECT plugin_id FROM scraper_plugins")}
            top = conn.execute("SELECT COALESCE(MAX(priority), 0) AS p FROM scraper_plugins").fetchone()["p"]
            for plugin_id in sorted(set(plugin_ids) - existing):
                top += 1
                conn.execute(
                    "INSERT INTO scraper_plugins(plugin_id, enabled, overwrite, priority, installed_at, updated_at) "
                    "VALUES (?, 0, 0, ?, ?, ?)",
                    (plugin_id, top, now, now),
                )

    def states(self) -> list[PluginState]:
        rows = self.db.query("SELECT * FROM scraper_plugins ORDER BY priority, plugin_id")
        return [PluginState(r["plugin_id"], bool(r["enabled"]), bool(r["overwrite"]), r["priority"]) for r in rows]

    def state(self, plugin_id: str) -> Optional[PluginState]:
        return next((s for s in self.states() if s.plugin_id == plugin_id), None)

    def _update(self, plugin_id: str, column: str, value: Any) -> None:
        with self.db.transaction() as conn:
            conn.execute(f"UPDATE scraper_plugins SET {column} = ?, updated_at = ? WHERE plugin_id = ?",
                         (value, now_iso(), plugin_id))

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        self._update(plugin_id, "enabled", 1 if enabled else 0)

    def set_overwrite(self, plugin_id: str, overwrite: bool) -> None:
        self._update(plugin_id, "overwrite", 1 if overwrite else 0)

    def reorder(self, ordered_ids: list[str]) -> None:
        """Persist an explicit 1..n priority for the given order."""
        now = now_iso()
        with self.db.transaction() as conn:
            for position, plugin_id in enumerate(ordered_ids, start=1):
                conn.execute("UPDATE scraper_plugins SET priority = ?, updated_at = ? WHERE plugin_id = ?",
                             (position, now, plugin_id))

    # -- plugin configuration ------------------------------------------------------------------
    def get_config(self, plugin_id: str) -> dict[str, Any]:
        """The plugin's stored settings (secrets included: never send this to the browser).

        Contract:
            Silences:
                - Stored JSON that does not parse, or is not an object, reads as ``{}``, the
                  same as "never configured".
        """
        row = self.db.query_one("SELECT config FROM scraper_config WHERE plugin_id = ?", (plugin_id,))
        if row is None:
            return {}
        try:
            data = json.loads(row["config"])
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def save_config(self, plugin_id: str, config: dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO scraper_config(plugin_id, config, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(plugin_id) DO UPDATE SET config = excluded.config, updated_at = excluded.updated_at",
                (plugin_id, json.dumps(config), now_iso()),
            )

    # -- plugin status -------------------------------------------------------------------------
    def get_status(self, plugin_id: str) -> dict[str, Any]:
        """The plugin's stored status; ``{}`` when there is none or it does not parse as an object."""
        row = self.db.query_one("SELECT status, updated_at FROM scraper_status WHERE plugin_id = ?", (plugin_id,))
        return _json_dict(row["status"]) if row is not None else {}

    def update_status(self, plugin_id: str, values: dict[str, Any]) -> dict[str, Any]:
        """Merge ``values`` into the stored status (a ``None`` value removes that entry).

        The read and the write share one write transaction, so concurrent updates for the same
        plugin are serialised and never lose each other's entries.

        Contract:
            Preconditions:
                - Values are JSON-serialisable (else ``TypeError`` from ``json.dumps``; nothing
                  is written) and contain no secrets (shown on the plugin page).
            Silences:
                - A stored status that does not parse is replaced by ``values`` alone.
        """
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status FROM scraper_status WHERE plugin_id = ?", (plugin_id,)).fetchone()
            status = _json_dict(row["status"]) if row is not None else {}
            for key, value in values.items():
                if value is None:
                    status.pop(key, None)
                else:
                    status[key] = value
            conn.execute(
                "INSERT INTO scraper_status(plugin_id, status, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(plugin_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
                (plugin_id, json.dumps(status), now_iso()),
            )
        return status

    # -- bindings / per-item scrape state --------------------------------------------------------
    def get_binding(self, plugin_id: str, item_kind: str, provider_id: int, category_id: str,
                    item_id: str) -> Optional[Binding]:
        row = self.db.query_one(
            "SELECT * FROM scraper_bindings WHERE plugin_id = ? AND item_kind = ? AND provider_id = ? "
            "AND category_id = ? AND item_id = ?",
            (plugin_id, item_kind, provider_id, category_id, item_id),
        )
        return self._binding(row) if row else None

    def bindings_for_provider(self, provider_id: int) -> dict[BindingKey, Binding]:
        rows = self.db.query("SELECT * FROM scraper_bindings WHERE provider_id = ?", (provider_id,))
        return {b.key: b for b in (self._binding(r) for r in rows)}

    @staticmethod
    def _binding(row: sqlite3.Row) -> Binding:
        """Build a :class:`Binding` from a ``scraper_bindings`` row.

        Contract:
            Silences:
                - Unparseable ``candidates`` JSON reads as ``[]``.
                - Unparseable or non-object ``manual_ids`` reads as ``{}``, and entries with an
                  empty namespace or value are dropped: a corrupt row silently loses its manual
                  lock, with no log. Keys and values are turned into ``str``.
        """
        data = {f.name: row[f.name] for f in fields(Binding)}
        try:
            data["candidates"] = json.loads(data["candidates"] or "[]")
        except ValueError:
            data["candidates"] = []
        try:
            manual = json.loads(data["manual_ids"] or "{}")
        except ValueError:
            manual = {}
        data["manual_ids"] = {str(k): str(v) for k, v in manual.items() if k and v} if isinstance(manual, dict) else {}
        return Binding(**data)

    def save_binding(self, binding: Binding) -> None:
        """Insert or overwrite the binding (whole record, keyed by :attr:`Binding.key`).

        Contract:
            Preconditions:
                - ``binding`` carries every field the caller wants kept (e.g. ``manual_ids``):
                  the stored row is overwritten, not merged.
                - ``candidates`` and ``manual_ids`` are JSON-serialisable (else ``TypeError``).
            Silences:
                - Only the first 10 ``candidates`` are stored; the rest are dropped.
        """
        data = asdict(binding)
        data["candidates"] = json.dumps(data["candidates"][:10])
        data["manual_ids"] = json.dumps(data["manual_ids"] or {}, sort_keys=True)
        columns = ", ".join(data)
        placeholders = ", ".join("?" for _ in data)
        updates = ", ".join(f"{c} = excluded.{c}" for c in data if c not in _BINDING_KEYS)
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO scraper_bindings ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT({', '.join(_BINDING_KEYS)}) DO UPDATE SET {updates}",
                tuple(data.values()),
            )

    def delete_binding(self, plugin_id: str, item_kind: str, provider_id: int, category_id: str, item_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM scraper_bindings WHERE plugin_id = ? AND item_kind = ? AND provider_id = ? "
                         "AND category_id = ? AND item_id = ?", (plugin_id, item_kind, provider_id, category_id, item_id))

    def item_bindings(self, item_kind: str, provider_id: int, category_id: str, item_id: str) -> list[Binding]:
        """Every plugin's binding for one item."""
        rows = self.db.query(
            "SELECT * FROM scraper_bindings WHERE item_kind = ? AND provider_id = ? AND category_id = ? AND item_id = ? "
            "ORDER BY plugin_id", (item_kind, provider_id, category_id, item_id))
        return [self._binding(r) for r in rows]

    def bindings_for_items(self, keys: Sequence[BindingItemKey]) -> list[Binding]:
        """The bindings of every plugin for the given ``(item_kind, provider_id, category_id, item_id)`` keys.

        Queried 200 keys at a time (4 parameters each, under SQLite's historical limit of 999).
        Keys in another order (e.g. an artwork ``ItemKey``) match nothing, silently.
        """
        result: list[Binding] = []
        for start in range(0, len(keys), 200):
            chunk = keys[start:start + 200]
            values = ", ".join("(?, ?, ?, ?)" for _ in chunk)
            rows = self.db.query(
                f"SELECT * FROM scraper_bindings WHERE (item_kind, provider_id, category_id, item_id) IN (VALUES {values})",
                tuple(v for key in chunk for v in key))
            result.extend(self._binding(r) for r in rows)
        return result

    def root_overview(self, plugin_ids: list[str], status: str = UNMATCHED, provider_id: Optional[int] = None,
                      item_kind: Optional[str] = None, title: str = "", limit: int = 50,
                      offset: int = 0) -> tuple[int, list[OverviewRow]]:
        """Active root items (movies and series, never seasons or episodes) in the requested state
        (see :data:`OVERVIEW_FILTERS`), judged on the bindings of ``plugin_ids`` only. Items no
        plugin in scope has tried are never listed. Returns the total and one page of item rows.

        Contract:
            Preconditions:
                - ``status`` is one of :data:`OVERVIEW_FILTERS`; any other value is used as a
                  binding status and simply matches nothing.
                - ``item_kind`` outside :data:`ROOT_KINDS` (e.g. ``None``) means both kinds.
                - ``title`` is matched as a case-insensitive substring (``LIKE`` wildcards in
                  it are escaped); blank means no title filter.
            Silences:
                - An empty ``plugin_ids`` returns ``(0, [])`` without querying.
        """
        if not plugin_ids:
            return 0, []
        negate = False
        cond_params: tuple[str, ...]
        if status == "manual":
            condition, cond_params = "b.manual_ids NOT IN ('', '{}')", ()
        elif status == UNMATCHED:  # tried, but no plugin in scope holds a match
            condition, cond_params, negate = HAS_MATCH, (), True
        elif status == MATCHED:
            condition, cond_params = HAS_MATCH, ()
        else:
            condition, cond_params = "b.status = ?", (status,)
        where: list[str] = ["1=1"]
        params: list[int | str] = []
        if provider_id is not None:
            where.append("i.provider_id = ?")
            params.append(provider_id)
        if item_kind in ROOT_KINDS:
            where.append("i.kind = ?")
            params.append(item_kind)
        if title.strip():
            where.append("i.title LIKE ? ESCAPE '\\'")
            params.append("%" + title.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        plugins = ", ".join("?" for _ in plugin_ids)
        base = f"""
            WITH items AS (
                SELECT 'movie' AS kind, provider_id, category_id, stream_id AS item_id, title, year
                FROM movies WHERE status = 'active'
                UNION ALL
                SELECT 'series' AS kind, provider_id, category_id, series_id AS item_id, title, year
                FROM series WHERE status = 'active'
            )
            SELECT {{columns}} FROM items i
            JOIN providers p ON p.id = i.provider_id
            LEFT JOIN categories c ON c.provider_id = i.provider_id AND c.content_type = i.kind
                                  AND c.category_id = i.category_id
            WHERE {' AND '.join(where)} AND {"NOT " if negate else ""}EXISTS (
                SELECT 1 FROM scraper_bindings b
                WHERE b.item_kind = i.kind AND b.provider_id = i.provider_id AND b.category_id = i.category_id
                  AND b.item_id = i.item_id AND b.plugin_id IN ({plugins}) AND {condition})
        """
        all_params = tuple(params) + tuple(plugin_ids) + tuple(cond_params)
        if negate:
            base += f"""AND EXISTS (
                SELECT 1 FROM scraper_bindings b
                WHERE b.item_kind = i.kind AND b.provider_id = i.provider_id AND b.category_id = i.category_id
                  AND b.item_id = i.item_id AND b.plugin_id IN ({plugins}))
            """
            all_params += tuple(plugin_ids)
        count = self.db.query_one(base.replace("{columns}", "COUNT(*) AS n"), all_params)
        assert count is not None  # an aggregate query always returns one row
        rows = self.db.query(
            base.replace("{columns}", "i.kind, i.provider_id, i.category_id, i.item_id, i.title, i.year, "
                                "p.name AS provider_name, c.name AS category_name")
            + " ORDER BY i.title COLLATE NOCASE, i.provider_id, i.kind, i.category_id, i.item_id LIMIT ? OFFSET ?",
            all_params + (int(limit), int(offset)))
        return int(count["n"]), [cast(OverviewRow, dict(r)) for r in rows]

    def root_match_summary(self, plugin_ids: list[str]) -> tuple[int, int]:
        """``(matched, total)`` over active movies + series (never episodes): an item is matched
        when one of ``plugin_ids`` (the enabled plugins) holds a match (see :data:`HAS_MATCH`),
        the same rule as the Matching page. Never-scraped items count in the total only."""
        plugins = ", ".join("?" for _ in plugin_ids)
        match = f"""EXISTS (
                SELECT 1 FROM scraper_bindings b
                WHERE b.item_kind = i.kind AND b.provider_id = i.provider_id AND b.category_id = i.category_id
                  AND b.item_id = i.item_id AND b.plugin_id IN ({plugins}) AND {HAS_MATCH})""" if plugin_ids else "0"
        row = self.db.query_one(f"""
            WITH items AS (
                SELECT 'movie' AS kind, provider_id, category_id, stream_id AS item_id
                FROM movies WHERE status = 'active'
                UNION ALL
                SELECT 'series' AS kind, provider_id, category_id, series_id AS item_id
                FROM series WHERE status = 'active'
            )
            SELECT COUNT(*) AS total, COALESCE(SUM(CASE WHEN {match} THEN 1 ELSE 0 END), 0) AS matched
            FROM items i""", tuple(plugin_ids))
        assert row is not None  # an aggregate query always returns one row
        return int(row["matched"]), int(row["total"])

    def binding_counts(self, provider_id: Optional[int] = None) -> dict[str, dict[str, int]]:
        where, params = ("WHERE provider_id = ?", (provider_id,)) if provider_id is not None else ("", ())
        rows = self.db.query(
            f"SELECT plugin_id, status, COUNT(*) AS n FROM scraper_bindings {where} GROUP BY plugin_id, status", params
        )
        result: dict[str, dict[str, int]] = {}
        for r in rows:
            result.setdefault(r["plugin_id"], {})[r["status"]] = int(r["n"])
        return result
