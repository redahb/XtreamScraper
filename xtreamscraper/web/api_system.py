"""Status, shutdown, settings, artwork settings, diagnostics and about routes."""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any, Optional

from flask import Blueprint, jsonify, request

from .. import __version__
from ..config.settings import ARTWORK_RECONCILE_KEYS, Settings, SettingsError
from ..probe.ffprobe_runner import ProbeError, ffprobe_version
from ..storage.summary import library_summary
from ..utils import logging_setup
from ..utils.redact import redact
from .api_common import ApiDeps, body, error, parse_int

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

log = logging.getLogger("xtreamscraper.web.api")

#: How long a shutdown waits for cancelled jobs before the server stops anyway (seconds).
SHUTDOWN_JOB_WAIT = 10.0


def register(api: Blueprint, deps: ApiDeps) -> None:
    ctx = deps.ctx
    shutdown_lock = threading.Lock()

    # -- status / dashboard ---------------------------------------------------------------------
    def summary() -> dict[str, Any]:
        plugins = ctx.scrapers.list()
        return library_summary(ctx.db, ctx.paths.db_path, len(plugins),
                               [p["plugin_id"] for p in plugins if p["enabled"]])

    @api.get("/status")
    def status() -> ResponseReturnValue:
        settings = ctx.settings_store.load()
        latest_sync = ctx.sync_history.latest_per_provider()
        latest_probe = ctx.probe_history.latest_per_provider()
        latest_metadata = ctx.metadata_history.latest_per_provider()
        path, how = ctx.jobs.ffprobe_location(settings)
        return jsonify(
            version=__version__,
            providers=[deps.provider_view(p, latest_sync.get(p.id), latest_probe.get(p.id), latest_metadata.get(p.id))
                       for p in ctx.providers.list()],
            jobs=ctx.jobs.jobs()[:10],
            ffprobe={"found": path is not None, "path": path, "how": how},
            scrapers=ctx.scrapers.list(),
            data_dir=ctx.paths.data_dir,
            db_path=ctx.paths.db_path,
            log_path=ctx.log_path,
            summary=summary(),
            shutting_down=bool(ctx.extra.get("shutting_down")),
        )

    @api.post("/shutdown")
    def shutdown() -> ResponseReturnValue:
        """Cancel running jobs, answer at once, then stop the server from a timer thread.

        Contract:
            Preconditions:
                - ``ctx.extra["shutdown"]`` must be a callable that stops the server (set by
                  ``app.py``, faked in tests); without one the answer is 503 and nothing changes.
            Silences:
                - A second request while ``shutting_down`` is set answers 200 with
                  ``jobs_cancelled: 0`` and does nothing else.
                - The timer thread waits for jobs at most ``SHUTDOWN_JOB_WAIT`` in total, then
                  stops the server even if some are still running. If the stop callback raises,
                  the error is logged and ``shutting_down`` is reset; the caller has already had
                  its 200.
        """
        stop = ctx.extra.get("shutdown")
        if not callable(stop):
            return error("Shutdown is not available in this mode", 503)
        with shutdown_lock:  # two simultaneous requests: only the first one shuts down
            if ctx.extra.get("shutting_down"):
                return jsonify(shutting_down=True, jobs_cancelled=0)
            ctx.extra["shutting_down"] = True
        cancelled = ctx.jobs.cancel_all()
        log.info("Shutdown requested from the WebUI (%d running job(s) cancelled)", cancelled)

        def run() -> None:  # after the response went out: let jobs stop, then stop the server
            ctx.jobs.wait_all(SHUTDOWN_JOB_WAIT)
            try:
                stop()
            except Exception:  # the server is still serving: say so, and let a later request retry
                log.exception("Stopping the web server failed")
                ctx.extra["shutting_down"] = False

        threading.Timer(0.3, run).start()
        return jsonify(shutting_down=True, jobs_cancelled=cancelled)

    @api.get("/about")
    def about() -> ResponseReturnValue:
        return jsonify(version=__version__, attributions=ctx.scrapers.attributions())

    # -- artwork ------------------------------------------------------------------------------------
    def artwork_view(settings: Settings) -> dict[str, Any]:
        return {k: v for k, v in settings.to_dict().items() if k.startswith("artwork_")}

    def reconcile_if_changed(previous: Settings, settings: Settings) -> Optional[dict[str, Any]]:
        """A changed artwork mode/type/alias setting reconciles the existing library (background job)."""
        if all(getattr(previous, k) == getattr(settings, k) for k in ARTWORK_RECONCILE_KEYS):
            return None
        if not ctx.providers.list():
            return None
        job = ctx.jobs.start_artwork(None)
        log.info("Artwork settings changed (mode %s); reconciliation started", settings.artwork_mode)
        return job.to_dict()

    @api.get("/artwork")
    def artwork_overview() -> ResponseReturnValue:
        settings = ctx.settings_store.load()
        return jsonify(settings=artwork_view(settings), counts=ctx.artwork.counts())

    @api.put("/artwork/settings")
    def save_artwork_settings() -> ResponseReturnValue:
        values = {k: v for k, v in body().items() if k.startswith("artwork_")}
        previous = ctx.settings_store.load()
        try:
            settings = ctx.settings_store.save(values)
        except SettingsError as exc:
            return error(str(exc))
        job = reconcile_if_changed(previous, settings)
        return jsonify(settings=artwork_view(settings), job=job)

    # -- settings / diagnostics -------------------------------------------------------------------
    @api.get("/settings")
    def get_settings() -> ResponseReturnValue:
        return jsonify(settings=ctx.settings_store.load().to_dict(),
                       active_port=ctx.extra.get("bind_port"), active_host=ctx.extra.get("bind_host"))

    @api.put("/settings")
    def save_settings() -> ResponseReturnValue:
        """Validate and store settings; a changed artwork setting starts a reconcile job.

        Contract:
            Silences:
                - Changed ``web_host``/``web_port`` only report ``restart_required``; the running
                  server keeps its address.
                - Invalid values answer 400 (``SettingsError``) and nothing is saved; a relative
                  ``default_target_folder`` is refused before validation.
        """
        data = body()
        previous = ctx.settings_store.load()
        folder = str(data.get("default_target_folder") or "").strip()
        if folder and not os.path.isabs(folder):
            return error("Default target folder must be an absolute path")
        try:
            settings = ctx.settings_store.save(data)
        except SettingsError as exc:
            return error(str(exc))
        logging_setup.set_level(settings.log_level)
        restart = settings.web_port != previous.web_port or settings.web_host != previous.web_host
        log.info("Settings saved%s", " (restart required for web server changes)" if restart else "")
        job = reconcile_if_changed(previous, settings)
        return jsonify(settings=settings.to_dict(), restart_required=restart, artwork_job=job)

    @api.post("/settings/test-ffprobe")
    def test_ffprobe() -> ResponseReturnValue:
        """Find and run the ffprobe that the typed (unsaved) or stored ``ffprobe_path`` gives.

        Contract:
            Preconditions:
                - The typed path is set on the freshly loaded ``Settings`` in place, without
                  ``coerce_settings``; that is safe because ``load()`` returns a new object and
                  both ``locate_ffprobe`` and ``ffprobe_version`` refuse network paths and other
                  program names themselves.
            Silences:
                - Every failure is a 200 with ``ok: false``: not found or refused (the locate
                  reason) or a ``ProbeError`` (redacted). Nothing is saved.
        """
        data = body()
        settings = ctx.settings_store.load()
        if "ffprobe_path" in data:
            settings.ffprobe_path = str(data.get("ffprobe_path") or "")
        path, how = ctx.jobs.ffprobe_location(settings)
        if path is None:
            return jsonify(ok=False, message=how, path=None)
        try:
            version = ffprobe_version(path)
        except ProbeError as exc:
            return jsonify(ok=False, message=redact(exc), path=path)
        return jsonify(ok=True, message=f"{version} ({how})", path=path)

    @api.get("/logs")
    def logs() -> ResponseReturnValue:
        limit = max(1, min(parse_int(request.args.get("limit"), "limit", 200), 500))
        return jsonify(lines=logging_setup.recent_lines(limit), log_path=ctx.log_path)
