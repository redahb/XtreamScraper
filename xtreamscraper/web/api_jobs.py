"""Job routes (sync, probe, scrape, artwork reconcile), job status and the history pages."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from flask import Blueprint, jsonify, request

from ..jobs.manager import KIND_SCOPES, BusyError, UnknownProviderError
from ..metadata.engine import ScrapeRequest
from ..probe.engine import ProbeRequest
from ..sync.engine import SyncRequest
from .api_common import ApiDeps, as_bool, body, error, optional_id, parse_int

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

    from ..jobs.progress import JobProgress
    from ..storage.history import _HistoryBase


def provider_arg(data: dict[str, Any]) -> Optional[int]:
    """``provider_id`` from a job body: absent, blank or ``"all"`` means every enabled provider.

    Contract:
        Raises:
            InputError: For any other value that is not a whole number (400 via the blueprint).
    """
    value = data.get("provider_id")
    return parse_int(value, "provider_id") if value not in (None, "", "all") else None


def history_limits() -> tuple[Optional[int], int, int]:
    """``(provider_id, limit, offset)`` from the query string: limit 1-500 (default 100), offset >= 0.

    Contract:
        Raises:
            InputError: For a ``limit`` or ``offset`` that is not a whole number.
        Silences:
            - Out-of-range numbers are clamped; an invalid ``provider_id`` means no filter.
    """
    limit = max(1, min(parse_int(request.args.get("limit"), "limit", 100), 500))
    offset = max(0, parse_int(request.args.get("offset"), "offset", 0))
    return optional_id(request.args.get("provider_id")), limit, offset


def start_job(start: Callable[[], JobProgress]) -> ResponseReturnValue:
    """Run ``start()`` and answer 202 with the job, 404 for an unknown provider, 409 when busy."""
    try:
        job = start()
    except UnknownProviderError:
        return error("Provider not found", 404)
    except BusyError as exc:
        return error(str(exc), 409)
    return jsonify(job=job.to_dict()), 202


def register(api: Blueprint, deps: ApiDeps) -> None:
    ctx = deps.ctx

    @api.post("/sync")
    def start_sync() -> ResponseReturnValue:
        data = body()
        scope = data.get("scope", "all")
        if scope not in ("all", "movies", "series"):
            return error("scope must be all, movies or series")
        req = SyncRequest(movies=scope in ("all", "movies"), series=scope in ("all", "series"),
                          full_refresh=as_bool(data.get("full_refresh")))
        return start_job(lambda: ctx.jobs.start_sync(provider_arg(data), req))

    @api.post("/probe")
    def start_probe() -> ResponseReturnValue:
        data = body()
        scope = data.get("scope", "all")
        if scope not in KIND_SCOPES:
            return error("scope must be all, movies or series")
        req = ProbeRequest(kinds=KIND_SCOPES[scope], force=as_bool(data.get("force")))
        return start_job(lambda: ctx.jobs.start_probe(provider_arg(data), req))

    @api.post("/metadata/scrape")
    def start_scrape() -> ResponseReturnValue:
        data = body()
        scope = data.get("scope", "all")
        kinds = {"all": ("movie", "series"), "movies": ("movie",), "series": ("series",)}.get(scope)
        if kinds is None:
            return error("scope must be all, movies or series")
        req = ScrapeRequest(kinds=kinds, force=as_bool(data.get("force")))
        return start_job(lambda: ctx.jobs.start_scrape(provider_arg(data), req))

    @api.post("/artwork/reconcile")
    def reconcile_artwork() -> ResponseReturnValue:
        data = body()
        return start_job(lambda: ctx.jobs.start_artwork(provider_arg(data), force=as_bool(data.get("force"))))

    @api.get("/jobs")
    def list_jobs() -> ResponseReturnValue:
        return jsonify(jobs=ctx.jobs.jobs())

    @api.get("/jobs/<job_id>")
    def get_job(job_id: str) -> ResponseReturnValue:
        job = ctx.jobs.get(job_id)
        return jsonify(job=job.to_dict()) if job else error("Job not found", 404)

    @api.post("/jobs/<job_id>/cancel")
    def cancel_job(job_id: str) -> ResponseReturnValue:
        return jsonify(cancelled=ctx.jobs.cancel(job_id))

    # -- history ------------------------------------------------------------------------------------
    def add_history(name: str, repo: Callable[[], _HistoryBase]) -> None:
        def listing() -> ResponseReturnValue:
            pid, limit, offset = history_limits()
            return jsonify(items=repo().list(pid, limit, offset))

        def item(row_id: int) -> ResponseReturnValue:
            found = repo().get(row_id)
            return jsonify(item=found) if found else error("Not found", 404)

        api.add_url_rule(f"/history/{name}", f"{name}_history", listing, methods=["GET"])
        api.add_url_rule(f"/history/{name}/<int:row_id>", f"{name}_history_item", item, methods=["GET"])

    add_history("sync", lambda: ctx.sync_history)
    add_history("probe", lambda: ctx.probe_history)
    add_history("metadata", lambda: ctx.metadata_history)
    add_history("artwork", lambda: ctx.jobs.artwork_history)
