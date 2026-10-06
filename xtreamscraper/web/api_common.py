"""Shared pieces of the JSON API: request helpers and the dependencies every route area uses."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from flask import jsonify, request
from werkzeug.exceptions import NotFound

from ..context import AppContext
from ..storage.categories import MOVIE, SERIES
from ..storage.providers import Provider
from ..sync.engine import client_for
from ..utils.redact import redact
from ..xtream.client import XtreamClient

if TYPE_CHECKING:
    from flask import Response


_INT = re.compile(r"[+-]?[0-9]{1,18}")
_INT_LIMIT = 2 ** 63 - 1  # SQLite INTEGER range


class InputError(ValueError):
    """Invalid request input; the API answers 400 with the message (no traceback is logged)."""


def parse_int(value: Any, name: str, default: Optional[int] = None) -> int:
    """An integer from JSON or a query string: ``5`` or ``"5"``; anything else raises :class:`InputError`.

    Contract:
        Preconditions:
            - ``None`` and ``""`` mean "absent": ``default`` is returned, or :class:`InputError`
              ("is required") is raised when there is no default. A ``default`` of ``0`` counts.
        Raises:
            InputError: For booleans, floats (even ``5.0``), ints beyond the SQLite range and
                strings that are not 1-18 ASCII digits with an optional sign (whitespace around
                is allowed). The message names ``name`` and holds no input.
    """
    if value is None or value == "":
        if default is None:
            raise InputError(f"{name} is required")
        return default
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) <= _INT_LIMIT:
        return value
    if isinstance(value, str) and _INT.fullmatch(value.strip()):
        return int(value.strip())
    raise InputError(f"{name} must be a whole number")


def optional_id(value: Any) -> Optional[int]:
    """A provider/row ID filter: a non-negative integer, else ``None`` (no filter).

    Contract:
        Silences:
            - Anything that is not 1-18 ASCII digits (``"abc"``, ``"-1"``, ``"5.0"``, ``True``,
              superscript digits) gives ``None``, which callers read as "all": a client bug
              widens the filter instead of failing.
    """
    text = str(value if value is not None else "").strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 18 else None


def body() -> dict[str, Any]:
    """The request's JSON object (``{}`` for anything else).

    Contract:
        Silences:
            - Malformed JSON, a non-JSON body and a JSON value that is not an object all give
              ``{}``, so a route sees "no fields" rather than an error.
    """
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def error(message: str, status: int = 400) -> tuple[Response, int]:
    return jsonify(error=redact(message)), status


def as_bool(value: Any, default: bool = False) -> bool:
    """A JSON flag: ``None`` gives ``default``; ``1``/``"true"``/``"yes"``/``"on"`` (any case) are true.

    Contract:
        Silences:
            - Every other value is ``False`` (``"nope"``, ``2``, ``{}``); a typo never raises.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")


def history_brief(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "status": item.get("status"),
        "started_at": item.get("started_at"),
        "finished_at": item.get("finished_at"),
        "duration_seconds": item.get("duration_seconds"),
        "error_count": item.get("error_count"),
        "warning_count": item.get("warning_count"),
        **{k: item.get(k) for k in ("created", "updated", "skipped", "missing", "considered", "probed",
                                    "succeeded", "failed", "sync_type", "scope", "matched", "unmatched",
                                    "ambiguous", "unchanged") if k in item},
    }


@dataclass
class ApiDeps:
    """What the route areas share: the application context plus a few provider helpers."""

    ctx: AppContext

    def provider_or_404(self, provider_id: int) -> Provider:
        provider = self.ctx.providers.get(provider_id)
        if provider is None:
            raise NotFound("Provider not found")
        return provider

    def make_client(self, provider: Provider, quick: bool = False) -> XtreamClient:
        """A client for ``provider`` with the current settings (``ctx.client_factory`` when set).

        ``quick`` is for requests the user waits on: read timeout at most 60 s, at most one retry.

        Contract:
            Preconditions:
                - ``quick`` changes the ``Settings`` object it just loaded in place. That is only
                  safe because ``SettingsStore.load()`` builds a fresh object on every call; a
                  cached settings object would carry the shorter limits into later jobs.
            Raises:
                sqlite3.Error: When the settings cannot be read (from ``load``, not caught).
        """
        settings = self.ctx.settings_store.load()
        if quick:
            settings.http_read_timeout = min(settings.http_read_timeout, 60)
            settings.http_retries = min(settings.http_retries, 1)
        if self.ctx.client_factory is not None:
            return self.ctx.client_factory(provider, settings)
        return client_for(provider, settings)

    def provider_view(self, provider: Provider, latest_sync: Optional[dict[str, Any]] = None,
                      latest_probe: Optional[dict[str, Any]] = None,
                      latest_metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """``provider.public_dict()`` plus counts, activity and the given latest history rows.

        Contract:
            Preconditions:
                - The view never holds the password (``public_dict`` leaves it out); keep it that
                  way when adding fields.
            Raises:
                sqlite3.Error: From the repositories (not caught).
        """
        ctx = self.ctx
        data = provider.public_dict()
        settings = ctx.settings_store.load()
        data["effective_target_folder"] = provider.target_folder or settings.default_target_folder
        data["activity"] = ctx.jobs.provider_activity().get(provider.id)
        data["selected_movie_categories"] = len(ctx.categories.selected(provider.id, MOVIE))
        data["selected_series_categories"] = len(ctx.categories.selected(provider.id, SERIES))
        data["counts"] = ctx.sync_state.counts(provider.id)
        data["probe_counts"] = ctx.probe_state.counts(provider.id)
        if latest_sync is not None:
            data["last_sync"] = history_brief(latest_sync)
        if latest_probe is not None:
            data["last_probe"] = history_brief(latest_probe)
        if latest_metadata is not None:
            data["last_metadata"] = history_brief(latest_metadata)
        return data
