"""Unmatched-media overview and manual matching of root movies and series.

Everything here is plugin-neutral: which IDs a user may enter, their syntax and how an
alternate ID becomes a plugin's own remote ID come from the plugins
(:meth:`~.plugin.ScraperPlugin.manual_id_fields`,
:meth:`~.plugin.ScraperPlugin.normalize_manual_id`,
:meth:`~.plugin.ScraperSession.resolve_manual_id`).

* **Overview.** Built only from the local database: library rows plus each installed plugin's
  stored root binding and candidates. Seasons and episodes are never listed, nor items that
  are missing at their provider.
* **Verify.** Normalizes the input through the plugin, resolves an alternate ID with that ID
  alone (never a title search) and fetches the movie or series. Nothing is stored.
* **Confirm.** Verifies again, then (holding the provider's job lock) writes the confirmed
  IDs into an existing NFO and stores a normal ``matched`` binding with
  ``match_method = manual_id``. The confirmed IDs (``Binding.manual_ids``) are the item's
  absolute truth: no plugin changes them (see :mod:`.engine`). A one-item scrape then fills
  the metadata through the normal engine.
* **Remove.** Takes the confirmed IDs back out of the NFO (only where they are unchanged) and
  deletes the binding, so the item is matched automatically again.

Any failed step leaves the previous binding and NFO as they were.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from ..filesystem import atomic
from ..nfo.document import NfoError
from ..nfo.metadata_writer import apply_manual_ids
from ..nfo.paths import NfoKind, movie_nfo_path, tvshow_nfo_path
from ..nfo.service import NfoOwner, update_nfo
from ..storage.db import Database
from ..storage.nfo_files import NfoFileRepository
from ..storage.scrapers import (
    API_ERROR,
    MATCHED,
    OVERVIEW_FILTERS,
    ROOT_KINDS,
    UNMATCHED,
    UNRESOLVED,
    Binding,
    ScraperStateRepository,
)
from ..utils.redact import redact
from ..utils.timeutil import now_iso
from .manager import ScraperManager
from .plugin import Capability, FieldType, ManualIdError, ManualIdField, ScraperPlugin, ScraperSession
from .results import FetchStatus, MatchMethod

log = logging.getLogger(__name__)

MOVIE, SERIES = ROOT_KINDS
CAPABILITY = {MOVIE: Capability.MOVIES, SERIES: Capability.SERIES}
KIND_LABEL = {MOVIE: "movie", SERIES: "series"}
MAX_LIMIT = 200


class ManualMatchError(Exception):
    """A request that cannot be carried out; ``message`` is safe to show."""

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class ItemRef:
    """One active root item of the library, identified by its stable Xtream IDs."""

    kind: str
    provider_id: int
    category_id: str
    item_id: str
    title: str
    year: Optional[int]
    nfo_path: str

    @property
    def binding_key(self) -> tuple:
        return (self.kind, self.provider_id, self.category_id, self.item_id)

    @property
    def owner_key(self) -> tuple:
        return (self.provider_id, self.kind, self.category_id, self.item_id)

    @property
    def nfo_kind(self) -> NfoKind:
        return NfoKind.MOVIE if self.kind == MOVIE else NfoKind.TVSHOW


@dataclass
class VerifiedMatch:
    plugin_id: str
    plugin_name: str
    item: ItemRef
    namespace: str
    namespace_label: str
    value: str  # the normalized ID the user entered
    remote_id: str  # the plugin's own ID it leads to
    title: Optional[str]
    year: Optional[int]
    external_ids: dict[str, str] = field(default_factory=dict)  # what the source lists for the title
    manual_ids: dict[str, str] = field(default_factory=dict)  # what becomes the item's truth

    def public_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id, "plugin_name": self.plugin_name, "item_kind": self.item.kind,
            "provider_id": self.item.provider_id, "category_id": self.item.category_id, "item_id": self.item.item_id,
            "namespace": self.namespace, "namespace_label": self.namespace_label, "value": self.value,
            "remote_id": self.remote_id, "title": self.title, "year": self.year,
            "external_ids": dict(self.external_ids), "manual_ids": dict(self.manual_ids),
        }


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return a is not None and b is not None and str(a).strip().casefold() == str(b).strip().casefold()


def _scalar(value: Any) -> Any:
    return value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None


class ManualMatchService:
    def __init__(self, db: Database, scrapers: ScraperManager, jobs: Any = None) -> None:
        self.db = db
        self.scrapers = scrapers
        self.jobs = jobs  # JobManager (provider locks, one-item scrape); optional in tests
        self.state = ScraperStateRepository(db)
        self.nfo_files = NfoFileRepository(db)

    # -- plugins -------------------------------------------------------------------------------------
    def _installed(self) -> list[dict[str, Any]]:
        """Installed plugins in priority order, with their enabled/configured state."""
        return self.scrapers.list()

    def _plugin(self, plugin_id: Any) -> ScraperPlugin:
        plugin = self.scrapers.registry.get(str(plugin_id or ""))
        if plugin is None:
            raise ManualMatchError("not_installed", "Scraper plugin not installed", 404)
        return plugin

    @staticmethod
    def _name(plugin: ScraperPlugin) -> str:
        return plugin.display_name or plugin.plugin_id

    @staticmethod
    def _fields(plugin: ScraperPlugin, kind: str) -> list[ManualIdField]:
        if not plugin.supports(CAPABILITY[kind]):
            return []
        try:
            fields = plugin.manual_id_fields(kind) or []
        except Exception:
            log.exception("Scraper %s: manual ID declaration failed", plugin.plugin_id)
            return []
        result, seen = [], set()
        for f in fields:
            namespace = (getattr(f, "namespace", "") or "").strip().lower()
            if isinstance(f, ManualIdField) and namespace and namespace not in seen:
                seen.add(namespace)
                result.append(ManualIdField(namespace, f.label or namespace, f.help, f.placeholder, f.pattern_hint))
        return result

    def capabilities(self) -> list[dict[str, Any]]:
        """Per installed plugin: the IDs a user may enter, per root media type."""
        result = []
        for entry in self._installed():
            plugin = self.scrapers.registry.get(entry["plugin_id"])
            if plugin is None:
                continue
            fields = {kind: [f.public_dict() for f in self._fields(plugin, kind)]
                      for kind in ROOT_KINDS if plugin.supports(CAPABILITY[kind])}
            result.append({
                "plugin_id": plugin.plugin_id, "name": entry["name"], "enabled": entry["enabled"],
                "configured": entry["configured"], "native_namespace": (plugin.id_namespace or "").lower() or None,
                "media_types": list(fields), "manual_id_fields": fields,
            })
        return result

    # -- overview ----------------------------------------------------------------------------------
    def overview(self, provider_id: Optional[int] = None, item_kind: Optional[str] = None,
                 plugin_id: Optional[str] = None, status: str = UNMATCHED, title: str = "",
                 limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """One page of root items in the requested state. Local data only; no source is contacted.

        Without ``plugin_id`` an item's state is judged on all enabled plugins: it is matched as
        soon as one of them matched it, unmatched when none did. With ``plugin_id``, on that
        plugin alone. Each item's ``matched`` flag is always the overall (enabled plugins) one."""
        if status not in OVERVIEW_FILTERS:
            raise ManualMatchError("invalid_filter", f"status must be one of: {', '.join(OVERVIEW_FILTERS)}")
        if item_kind not in (None, "", *ROOT_KINDS):
            raise ManualMatchError("invalid_filter", "type must be movie or series")
        installed = self._installed()
        order = [p["plugin_id"] for p in installed]
        enabled = [p["plugin_id"] for p in installed if p["enabled"]]
        names = {p["plugin_id"]: p["name"] for p in installed}
        if plugin_id:
            if plugin_id not in names:
                raise ManualMatchError("not_installed", "Scraper plugin not installed", 404)
            scope = [plugin_id]
        else:
            scope = enabled
        limit = max(1, min(int(limit), MAX_LIMIT))
        offset = max(0, int(offset))
        total, rows = self.state.root_overview(scope, status, provider_id, item_kind or None, title or "",
                                               limit, offset)
        keys = [(r["kind"], r["provider_id"], r["category_id"], r["item_id"]) for r in rows]
        by_item: dict[tuple, dict[str, Binding]] = defaultdict(dict)
        for b in self.state.bindings_for_items(keys):
            if b.plugin_id in names:
                by_item[(b.item_kind, b.provider_id, b.category_id, b.item_id)][b.plugin_id] = b
        items = []
        for r, key in zip(rows, keys):
            bindings = by_item.get(key, {})
            attempts = [b.last_attempt for b in bindings.values() if b.last_attempt]
            items.append({
                "item_kind": r["kind"], "provider_id": r["provider_id"], "provider_name": r["provider_name"],
                "category_id": r["category_id"], "category_name": r["category_name"], "item_id": r["item_id"],
                "title": r["title"], "year": r["year"], "last_attempt": max(attempts) if attempts else None,
                "matched": any(_has_match(bindings[pid]) for pid in enabled if pid in bindings),
                "plugins": [self._binding_view(bindings[pid], names[pid]) for pid in order if pid in bindings],
            })
        return {"total": total, "limit": limit, "offset": offset, "items": items}

    @staticmethod
    def _binding_view(b: Binding, name: str) -> dict[str, Any]:
        candidates = []
        for c in b.candidates or []:
            if isinstance(c, dict) and _scalar(c.get("remote_id")) is not None:
                candidates.append({"remote_id": str(c["remote_id"]), "title": _scalar(c.get("title")),
                                   "year": _scalar(c.get("year")), "score": _scalar(c.get("score"))})
        return {
            "plugin_id": b.plugin_id, "name": name, "status": b.status, "actionable": b.status in UNRESOLVED,
            "message": redact(b.message) if b.message else None, "last_attempt": b.last_attempt,
            "remote_id": b.remote_id, "matched_title": b.matched_title, "matched_year": b.matched_year,
            "match_method": b.match_method, "manual": b.manual, "manual_ids": dict(b.manual_ids),
            "candidates": candidates,
        }

    # -- the local item ----------------------------------------------------------------------------
    def _item(self, body: dict[str, Any]) -> ItemRef:
        kind = body.get("item_kind")
        category_id, item_id = body.get("category_id"), body.get("item_id")
        try:
            provider_id = int(body.get("provider_id"))
        except (TypeError, ValueError):
            provider_id = None
        if kind not in ROOT_KINDS or provider_id is None or not isinstance(category_id, (str, int)) \
                or not isinstance(item_id, (str, int)) or isinstance(category_id, bool) or isinstance(item_id, bool) \
                or not str(category_id).strip() or not str(item_id).strip():
            raise ManualMatchError("invalid_item", "Identify the item by item_kind (movie or series), provider_id, "
                                                   "category_id and item_id")
        category_id, item_id = str(category_id), str(item_id)
        if kind == MOVIE:
            row = self.db.query_one("SELECT title, year, strm_path AS path, status FROM movies "
                                    "WHERE provider_id = ? AND category_id = ? AND stream_id = ?",
                                    (provider_id, category_id, item_id))
        else:
            row = self.db.query_one("SELECT title, year, folder_path AS path, status FROM series "
                                    "WHERE provider_id = ? AND category_id = ? AND series_id = ?",
                                    (provider_id, category_id, item_id))
        if row is None:
            raise ManualMatchError("item_not_found", "This item is not in the library", 404)
        if row["status"] != "active":
            raise ManualMatchError("item_missing", "This item is missing at its provider, so it cannot be matched", 409)
        nfo = movie_nfo_path(os.path.dirname(row["path"])) if kind == MOVIE else tvshow_nfo_path(row["path"])
        return ItemRef(kind, provider_id, category_id, item_id, row["title"], row["year"], nfo)

    # -- verification --------------------------------------------------------------------------------
    def verify(self, body: dict[str, Any]) -> VerifiedMatch:
        """Check an entered ID with the plugin and fetch what it points to. Stores nothing."""
        plugin = self._plugin(body.get("plugin_id"))
        name = self._name(plugin)
        item = self._item(body)
        if not plugin.supports(CAPABILITY[item.kind]):
            raise ManualMatchError("unsupported_media", f"{name} does not scrape {KIND_LABEL[item.kind]}")
        fields = {f.namespace: f for f in self._fields(plugin, item.kind)}
        namespace = str(body.get("namespace") or "").strip().lower()
        id_field = fields.get(namespace)
        if id_field is None:
            raise ManualMatchError("unsupported_namespace", f"{name} does not accept this ID type")
        raw = body.get("value")
        if isinstance(raw, bool) or not isinstance(raw, (str, int)) or not str(raw).strip():
            raise ManualMatchError("invalid_format", f"Enter a {id_field.label}")
        try:
            value = str(plugin.normalize_manual_id(namespace, str(raw)) or "").strip()
        except ManualIdError as exc:
            raise ManualMatchError("invalid_format", str(exc) or f"Not a valid {id_field.label}") from None
        except Exception:
            log.exception("Scraper %s: manual ID normalization failed", plugin.plugin_id)
            value = ""
        if not value:
            raise ManualMatchError("invalid_format", f"Not a valid {id_field.label}")

        config = self.scrapers.state.get_config(plugin.plugin_id)
        if not plugin.is_configured(config):
            raise ManualMatchError("not_configured", f"{name} is not configured. Set it up on the Metadata Scrapers "
                                                     "page; the ID cannot be checked until then.", 409)
        secrets = [config.get(f.key) for f in plugin.config_schema() if f.type is FieldType.SECRET]
        try:
            session = plugin.create_session(config)
        except Exception as exc:
            raise ManualMatchError("plugin_unavailable", redact(f"{name} could not start: {exc}", secrets), 502) from None
        try:
            native = (plugin.id_namespace or "").strip().lower()
            remote_id = value if namespace == native else self._resolve(session, name, item, id_field, value, secrets)
            meta = self._fetch(session, name, item, id_field, value, remote_id, secrets)
        except ManualMatchError:
            raise
        except Exception as exc:
            log.warning("Scraper %s raised during manual matching: %s", plugin.plugin_id, redact(exc, secrets))
            raise ManualMatchError("api_error", redact(f"{name} failed: {exc}", secrets), 502) from None
        finally:
            self._finish(plugin.plugin_id, session, secrets)

        manual_ids = {native: remote_id} if native else {}
        manual_ids.setdefault(namespace, value)
        external_ids = {str(k).strip().lower(): str(v) for k, v in (meta.external_ids or {}).items()
                        if k and _scalar(v) not in (None, "")}
        return VerifiedMatch(plugin.plugin_id, name, item, namespace, id_field.label, value, remote_id,
                             meta.title, meta.year, external_ids, manual_ids)

    @staticmethod
    def _failure(session: ScraperSession, name: str, message: Optional[str], secrets: list) -> ManualMatchError:
        text = session.suspended or message or "request failed"
        return ManualMatchError("api_error", redact(f"{name}: {text}", secrets), 502)

    def _resolve(self, session: ScraperSession, name: str, item: ItemRef, id_field: ManualIdField, value: str,
                 secrets: list) -> str:
        outcome = session.resolve_manual_id(item.kind, id_field.namespace, value)
        if outcome.status is FetchStatus.OK and outcome.remote_id:
            return str(outcome.remote_id)
        if outcome.status is FetchStatus.NOT_FOUND:
            detail = f": {outcome.message}" if outcome.message else ""
            raise ManualMatchError("not_found", redact(f"{name} found no {KIND_LABEL[item.kind]} for {id_field.label} "
                                                       f"{value}{detail}", secrets), 404)
        if outcome.status is FetchStatus.API_ERROR:
            raise self._failure(session, name, outcome.message, secrets)
        raise ManualMatchError("unsupported_namespace", f"{name} cannot look up a {id_field.label}")

    def _fetch(self, session: ScraperSession, name: str, item: ItemRef, id_field: ManualIdField, value: str,
               remote_id: str, secrets: list):
        fetch = session.get_movie if item.kind == MOVIE else session.get_series
        outcome = fetch(remote_id)
        if outcome.status is FetchStatus.OK and outcome.metadata is not None:
            return outcome.metadata
        if outcome.status is FetchStatus.NOT_FOUND:
            detail = f": {outcome.message}" if outcome.message else ""
            raise ManualMatchError("not_found", redact(f"{name} found no {KIND_LABEL[item.kind]} for {id_field.label} "
                                                       f"{value}{detail}", secrets), 404)
        if outcome.status is FetchStatus.API_ERROR:
            raise self._failure(session, name, outcome.message, secrets)
        raise ManualMatchError("unsupported_media", f"{name} cannot fetch {KIND_LABEL[item.kind]} by ID")

    def _finish(self, plugin_id: str, session: ScraperSession, secrets: list) -> None:
        try:
            update = session.status_update()
            if update:
                self.scrapers.record_status(plugin_id, update, secrets)
        except Exception:
            log.exception("Recording the status of scraper %s failed", plugin_id)
        try:
            session.close()
        except Exception:
            log.exception("Closing scraper session failed")

    def preview(self, body: dict[str, Any]) -> dict[str, Any]:
        """``verify`` plus the match it would replace, for the confirmation step."""
        verified = self.verify(body)
        result = verified.public_dict()
        current = self.state.get_binding(verified.plugin_id, *verified.item.binding_key)
        result["current"] = ({"remote_id": current.remote_id, "title": current.matched_title,
                              "year": current.matched_year, "manual": current.manual}
                             if current and current.remote_id else None)
        return result

    # -- assignment ------------------------------------------------------------------------------------
    @contextmanager
    def _locked(self, item: ItemRef) -> Iterator[None]:
        if self.jobs is None:
            yield
            return
        from ..jobs.manager import BusyError

        try:
            with self.jobs.provider_locked(item.provider_id, "manual matching"):
                yield
        except BusyError as exc:
            raise ManualMatchError("busy", str(exc), 409) from None

    def _other_locks(self, item: ItemRef, plugin_id: str) -> dict[str, tuple[str, str]]:
        """IDs set by hand through the item's other installed plugins: namespace -> (value, plugin)."""
        locks: dict[str, tuple[str, str]] = {}
        for b in self.state.item_bindings(*item.binding_key):
            if b.plugin_id == plugin_id or not b.manual_ids or b.status not in (MATCHED, API_ERROR):
                continue
            if self.scrapers.registry.get(b.plugin_id) is None:
                continue
            for namespace, value in b.manual_ids.items():
                locks.setdefault(namespace.lower(), (value, b.plugin_id))
        return locks

    def _edit_nfo(self, item: ItemRef, assign: dict[str, str], remove: dict[str, str]) -> None:
        if not atomic.is_file(item.nfo_path) or (not assign and not remove):
            return  # the next scrape writes the IDs into a new NFO
        try:
            update_nfo(item.nfo_path, item.nfo_kind, lambda doc: apply_manual_ids(doc, assign, remove),
                       owner=NfoOwner(self.nfo_files, item.owner_key))
        except (NfoError, OSError) as exc:
            raise ManualMatchError("nfo_failed", f"The NFO file could not be updated, so nothing was changed: {exc}",
                                   409) from None

    def assign(self, body: dict[str, Any]) -> dict[str, Any]:
        """Verify again, then store the confirmed ID and scrape the item."""
        verified = self.verify(body)
        item, plugin = verified.item, self._plugin(verified.plugin_id)
        with self._locked(item):
            others = self._other_locks(item, plugin.plugin_id)
            for namespace, value in verified.manual_ids.items():
                if namespace in others and not _same(others[namespace][0], value):
                    other_value, other_plugin = others[namespace]
                    other = self.scrapers.registry.get(other_plugin)
                    raise ManualMatchError(
                        "conflict", f"This {KIND_LABEL[item.kind]} already has {namespace} ID {other_value}, set by "
                                    f"hand for {self._name(other) if other else other_plugin}. Change or remove that "
                                    "match first.", 409)
            old = self.state.get_binding(plugin.plugin_id, *item.binding_key)
            stale = {ns: v for ns, v in (old.manual_ids if old else {}).items()
                     if not _same(verified.manual_ids.get(ns), v) and not _same(others.get(ns, (None,))[0], v)}
            self._edit_nfo(item, verified.manual_ids, stale)
            config = self.scrapers.state.get_config(plugin.plugin_id)
            now = now_iso()
            self.state.save_binding(Binding(
                plugin_id=plugin.plugin_id, item_kind=item.kind, provider_id=item.provider_id,
                category_id=item.category_id, item_id=item.item_id, status=MATCHED, remote_id=verified.remote_id,
                match_method=MatchMethod.MANUAL_ID.value, matched_title=verified.title, matched_year=verified.year,
                matched_at=now, last_attempt=now, config_fingerprint=self.scrapers.config_fingerprint(plugin, config),
                manual_ids=dict(verified.manual_ids),
            ))
        log.info("Manual match: %s %s (%s) -> %s %s", KIND_LABEL[item.kind], item.title, item.item_id,
                 plugin.plugin_id, verified.remote_id)
        job, note = self._scrape(item, plugin)
        return {"match": verified.public_dict(), "job": job, "note": note}

    def remove(self, body: dict[str, Any]) -> dict[str, Any]:
        """Withdraw a manual match: the item is matched automatically again."""
        plugin = self._plugin(body.get("plugin_id"))
        item = self._item(body)
        with self._locked(item):
            old = self.state.get_binding(plugin.plugin_id, *item.binding_key)
            if old is None or not old.manual_ids:
                raise ManualMatchError("no_manual_match", "There is no match set by hand to remove", 409)
            others = self._other_locks(item, plugin.plugin_id)
            withdraw = {ns: v for ns, v in old.manual_ids.items() if not _same(others.get(ns, (None,))[0], v)}
            self._edit_nfo(item, {}, withdraw)
            self.state.delete_binding(plugin.plugin_id, *item.binding_key)
        log.info("Manual match removed: %s %s (%s), %s", KIND_LABEL[item.kind], item.title, item.item_id,
                 plugin.plugin_id)
        job, note = self._scrape(item, plugin)
        return {"removed": True, "job": job, "note": note}

    def _scrape(self, item: ItemRef, plugin: ScraperPlugin) -> tuple[Optional[dict], Optional[str]]:
        """Start the one-item scrape when the plugin can run; otherwise say when it happens."""
        name = self._name(plugin)
        entry = next((p for p in self._installed() if p["plugin_id"] == plugin.plugin_id), None)
        if self.jobs is None:
            return None, None
        if not entry or not entry["enabled"]:
            return None, f"{name} is disabled, so its details and artwork are replaced by the first scrape after you enable it."
        from ..jobs.manager import BusyError

        try:
            job = self.jobs.start_item_scrape(item.provider_id, item.kind, item.category_id, item.item_id, item.title)
        except BusyError:
            return None, "A job is running for this provider; the next metadata scrape replaces the details and artwork."
        return job.to_dict(), None


def _has_match(b: Binding) -> bool:
    """The binding holds a match (an API error keeps the stored one); see ``HAS_MATCH``."""
    return b.status == MATCHED or (b.status == API_ERROR and bool(b.remote_id))
