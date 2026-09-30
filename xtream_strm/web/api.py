"""JSON API used by the WebUI.

Provider passwords are write-only: they can be set but are never returned.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from flask import Blueprint, jsonify, request

from .. import __version__
from ..config.settings import SettingsError
from ..context import AppContext
from ..jobs.manager import KIND_SCOPES, BusyError
from ..metadata.registry import registry as metadata_registry
from ..probe.engine import ProbeRequest
from ..probe.ffprobe import ProbeError, ffprobe_version
from ..storage.categories import CONTENT_TYPES, MOVIE, SERIES
from ..storage.providers import Provider, ProviderValidationError
from ..sync.engine import SyncRequest, client_for
from ..utils import logging_setup
from ..utils.redact import redact
from ..xtream.client import XtreamError

log = logging.getLogger(__name__)


def _body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _error(message: str, status: int = 400):
    return jsonify(error=redact(message)), status


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")


def build_api(ctx: AppContext) -> Blueprint:
    api = Blueprint("api", __name__)

    def provider_or_404(provider_id: int) -> Provider:
        provider = ctx.providers.get(provider_id)
        if provider is None:
            from werkzeug.exceptions import NotFound

            raise NotFound("Provider not found")
        return provider

    def make_client(provider: Provider, quick: bool = False):
        settings = ctx.settings_store.load()
        if quick:
            settings.http_read_timeout = min(settings.http_read_timeout, 60)
            settings.http_retries = min(settings.http_retries, 1)
        if ctx.client_factory is not None:
            return ctx.client_factory(provider, settings)
        return client_for(provider, settings)

    def provider_view(provider: Provider, latest_sync=None, latest_probe=None) -> dict:
        data = provider.public_dict()
        settings = ctx.settings_store.load()
        data["effective_target_folder"] = provider.target_folder or settings.default_target_folder
        data["activity"] = ctx.jobs.provider_activity().get(provider.id)
        data["selected_movie_categories"] = len(ctx.categories.selected(provider.id, MOVIE))
        data["selected_series_categories"] = len(ctx.categories.selected(provider.id, SERIES))
        data["counts"] = ctx.sync_state.counts(provider.id)
        data["probe_counts"] = ctx.probe_state.counts(provider.id)
        if latest_sync is not None:
            data["last_sync"] = _history_brief(latest_sync)
        if latest_probe is not None:
            data["last_probe"] = _history_brief(latest_probe)
        return data

    # -- status / dashboard ---------------------------------------------------------------------
    @api.get("/status")
    def status():
        settings = ctx.settings_store.load()
        latest_sync = ctx.sync_history.latest_per_provider()
        latest_probe = ctx.probe_history.latest_per_provider()
        path, how = ctx.jobs.ffprobe_location(settings)
        return jsonify(
            version=__version__,
            providers=[provider_view(p, latest_sync.get(p.id), latest_probe.get(p.id)) for p in ctx.providers.list()],
            jobs=ctx.jobs.jobs()[:10],
            ffprobe={"found": path is not None, "path": path, "how": how},
            metadata_providers=metadata_registry.describe(),
            data_dir=ctx.paths.data_dir,
            db_path=ctx.paths.db_path,
            log_path=ctx.log_path,
        )

    # -- providers ---------------------------------------------------------------------------------
    @api.get("/providers")
    def list_providers():
        return jsonify(providers=[provider_view(p) for p in ctx.providers.list()])

    @api.post("/providers")
    def create_provider():
        try:
            provider = ctx.providers.create(_body())
        except ProviderValidationError as exc:
            return _error(str(exc))
        log.info("Provider added: %s (id %d)", provider.name, provider.id)
        return jsonify(provider=provider_view(provider)), 201

    @api.get("/providers/<int:provider_id>")
    def get_provider(provider_id: int):
        return jsonify(provider=provider_view(provider_or_404(provider_id)))

    @api.put("/providers/<int:provider_id>")
    def update_provider(provider_id: int):
        provider_or_404(provider_id)
        if ctx.jobs.is_busy(provider_id):
            return _error("A job is running for this provider; try again when it has finished", 409)
        try:
            provider = ctx.providers.update(provider_id, _body())
        except ProviderValidationError as exc:
            return _error(str(exc))
        log.info("Provider updated: %s (id %d)", provider.name, provider.id)
        return jsonify(provider=provider_view(provider))

    @api.delete("/providers/<int:provider_id>")
    def delete_provider(provider_id: int):
        provider = provider_or_404(provider_id)
        if ctx.jobs.is_busy(provider_id):
            return _error("A job is running for this provider; cancel it first", 409)
        ctx.providers.delete(provider_id)
        log.info("Provider deleted: %s (id %d); library files were left on disk", provider.name, provider_id)
        return jsonify(deleted=True)

    @api.post("/providers/<int:provider_id>/enabled")
    def set_enabled(provider_id: int):
        provider_or_404(provider_id)
        ctx.providers.set_enabled(provider_id, _bool(_body().get("enabled"), True))
        return jsonify(provider=provider_view(provider_or_404(provider_id)))

    def _test(provider: Provider) -> dict:
        try:
            with make_client(provider, quick=True) as client:
                account = client.authenticate()
        except XtreamError as exc:
            return {"ok": False, "message": redact(str(exc), (provider.username, provider.password))}
        except Exception as exc:
            log.exception("Connection test failed")
            return {"ok": False, "message": redact(f"Unexpected error: {exc}", (provider.username, provider.password))}
        return {"ok": account.authenticated, "message": account.summary()}

    @api.post("/providers/test")
    def test_unsaved():
        """Test credentials from the edit form before saving (blank password = stored one)."""
        body = _body()
        stored = ctx.providers.get(int(body["id"])) if str(body.get("id") or "").isdigit() else None
        from ..xtream.urls import normalize_base_url

        candidate = Provider(
            id=stored.id if stored else 0,
            name=str(body.get("name") or (stored.name if stored else "test")),
            base_url=normalize_base_url(str(body.get("base_url") or (stored.base_url if stored else ""))),
            username=str(body.get("username") or (stored.username if stored else "")),
            password=str(body.get("password") or (stored.password if stored else "")),
            user_agent=str(body.get("user_agent") if body.get("user_agent") is not None else (stored.user_agent if stored else "")),
        )
        if not candidate.base_url or not candidate.username or not candidate.password:
            return _error("Base URL, username and password are required to test")
        return jsonify(_test(candidate))

    @api.post("/providers/<int:provider_id>/test")
    def test_provider(provider_id: int):
        provider = provider_or_404(provider_id)
        result = _test(provider)
        ctx.providers.record_test(provider_id, result["ok"], result["message"])
        level = logging.INFO if result["ok"] else logging.WARNING
        log.log(level, "Connection test for %s: %s", provider.name, result["message"])
        return jsonify(result)

    # -- categories ----------------------------------------------------------------------------------
    @api.get("/providers/<int:provider_id>/categories")
    def get_categories(provider_id: int):
        provider = provider_or_404(provider_id)
        return jsonify(
            refreshed_at=provider.categories_refreshed_at,
            movie=[c.public_dict() for c in ctx.categories.list(provider_id, MOVIE)],
            series=[c.public_dict() for c in ctx.categories.list(provider_id, SERIES)],
        )

    @api.post("/providers/<int:provider_id>/categories/refresh")
    def refresh_categories(provider_id: int):
        provider = provider_or_404(provider_id)
        wanted = _body().get("content_type")
        types = [wanted] if wanted in CONTENT_TYPES else list(CONTENT_TYPES)
        errors, warnings = [], []
        with make_client(provider, quick=True) as client:
            for content_type in types:
                try:
                    fetch = client.get_vod_categories if content_type == MOVIE else client.get_series_categories
                    categories = fetch(warnings)
                except XtreamError as exc:
                    errors.append(f"{content_type}: {exc}")
                    log.warning("Category refresh failed for %s: %s", provider.name, exc)
                    continue
                ctx.categories.replace_from_provider(
                    provider_id, content_type, [(c.category_id, c.name) for c in categories]
                )
        if len(errors) < len(types):
            ctx.providers.mark_categories_refreshed(provider_id)
        result = get_categories(provider_id).get_json()
        result.update(errors=[redact(e) for e in errors], warnings=[redact(w) for w in warnings[:50]])
        return jsonify(result), (502 if len(errors) == len(types) else 200)

    @api.put("/providers/<int:provider_id>/categories")
    def save_categories(provider_id: int):
        provider_or_404(provider_id)
        body = _body()
        for content_type in CONTENT_TYPES:
            if content_type in body:
                ids = body[content_type]
                if not isinstance(ids, list):
                    return _error(f"'{content_type}' must be a list of category IDs")
                ctx.categories.set_selection(provider_id, content_type, [str(i) for i in ids])
        return get_categories(provider_id)

    # -- jobs ----------------------------------------------------------------------------------------
    def _provider_arg(body: dict) -> Optional[int]:
        value = body.get("provider_id")
        return int(value) if value not in (None, "", "all") else None

    @api.post("/sync")
    def start_sync():
        body = _body()
        scope = body.get("scope", "all")
        if scope not in ("all", "movies", "series"):
            return _error("scope must be all, movies or series")
        req = SyncRequest(movies=scope in ("all", "movies"), series=scope in ("all", "series"),
                          full_refresh=_bool(body.get("full_refresh")))
        try:
            job = ctx.jobs.start_sync(_provider_arg(body), req)
        except KeyError:
            return _error("Provider not found", 404)
        except BusyError as exc:
            return _error(str(exc), 409)
        return jsonify(job=job.to_dict()), 202

    @api.post("/probe")
    def start_probe():
        body = _body()
        scope = body.get("scope", "all")
        if scope not in KIND_SCOPES:
            return _error("scope must be all, movies or series")
        req = ProbeRequest(kinds=KIND_SCOPES[scope], force=_bool(body.get("force")))
        try:
            job = ctx.jobs.start_probe(_provider_arg(body), req)
        except KeyError:
            return _error("Provider not found", 404)
        except BusyError as exc:
            return _error(str(exc), 409)
        return jsonify(job=job.to_dict()), 202

    @api.get("/jobs")
    def list_jobs():
        return jsonify(jobs=ctx.jobs.jobs())

    @api.get("/jobs/<job_id>")
    def get_job(job_id: str):
        job = ctx.jobs.get(job_id)
        return jsonify(job=job.to_dict()) if job else _error("Job not found", 404)

    @api.post("/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        return jsonify(cancelled=ctx.jobs.cancel(job_id))

    # -- history ------------------------------------------------------------------------------------
    def _limits():
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
        offset = max(0, int(request.args.get("offset", 0)))
        pid = request.args.get("provider_id")
        return (int(pid) if pid and pid.isdigit() else None), limit, offset

    @api.get("/history/sync")
    def sync_history():
        pid, limit, offset = _limits()
        return jsonify(items=ctx.sync_history.list(pid, limit, offset))

    @api.get("/history/sync/<int:row_id>")
    def sync_history_item(row_id: int):
        item = ctx.sync_history.get(row_id)
        return jsonify(item=item) if item else _error("Not found", 404)

    @api.get("/history/probe")
    def probe_history():
        pid, limit, offset = _limits()
        return jsonify(items=ctx.probe_history.list(pid, limit, offset))

    @api.get("/history/probe/<int:row_id>")
    def probe_history_item(row_id: int):
        item = ctx.probe_history.get(row_id)
        return jsonify(item=item) if item else _error("Not found", 404)

    # -- settings / diagnostics -------------------------------------------------------------------
    @api.get("/settings")
    def get_settings():
        return jsonify(settings=ctx.settings_store.load().to_dict(),
                       active_port=ctx.extra.get("bind_port"), active_host=ctx.extra.get("bind_host"))

    @api.put("/settings")
    def save_settings():
        body = _body()
        previous = ctx.settings_store.load()
        folder = str(body.get("default_target_folder") or "").strip()
        if folder and not os.path.isabs(folder):
            return _error("Default target folder must be an absolute path")
        try:
            settings = ctx.settings_store.save(body)
        except SettingsError as exc:
            return _error(str(exc))
        logging_setup.set_level(settings.log_level)
        restart = settings.web_port != previous.web_port or settings.web_host != previous.web_host
        log.info("Settings saved%s", " (restart required for web server changes)" if restart else "")
        return jsonify(settings=settings.to_dict(), restart_required=restart)

    @api.post("/settings/test-ffprobe")
    def test_ffprobe():
        body = _body()
        settings = ctx.settings_store.load()
        if "ffprobe_path" in body:
            settings.ffprobe_path = str(body.get("ffprobe_path") or "")
        path, how = ctx.jobs.ffprobe_location(settings)
        if path is None:
            return jsonify(ok=False, message=how, path=None)
        try:
            version = ffprobe_version(path)
        except ProbeError as exc:
            return jsonify(ok=False, message=str(exc), path=path)
        return jsonify(ok=True, message=f"{version} ({how})", path=path)

    @api.get("/logs")
    def logs():
        limit = max(1, min(int(request.args.get("limit", 200)), 500))
        return jsonify(lines=logging_setup.recent_lines(limit), log_path=ctx.log_path)

    return api


def _history_brief(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "status": item.get("status"),
        "started_at": item.get("started_at"),
        "finished_at": item.get("finished_at"),
        "duration_seconds": item.get("duration_seconds"),
        "error_count": item.get("error_count"),
        "warning_count": item.get("warning_count"),
        **{k: item.get(k) for k in ("created", "updated", "skipped", "missing", "considered", "probed",
                                    "succeeded", "failed", "sync_type", "scope") if k in item},
    }
