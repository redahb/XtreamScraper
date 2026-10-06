"""Unmatched media and manual matching routes (verify, assign, remove)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Blueprint, jsonify, request

from ..metadata.manual_matching import ManualMatchError
from ..utils.redact import redact
from .api_common import ApiDeps, InputError, as_bool, body, error, optional_id, parse_int

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue


def register(api: Blueprint, deps: ApiDeps) -> None:
    ctx = deps.ctx

    def manual_error(exc: ManualMatchError) -> ResponseReturnValue:
        return jsonify(error=redact(exc.message), code=exc.code), exc.http_status

    @api.get("/metadata/manual-id-types")
    def manual_id_types() -> ResponseReturnValue:
        return jsonify(plugins=ctx.manual_matching.capabilities())

    @api.get("/metadata/unmatched")
    def unmatched() -> ResponseReturnValue:
        """The Matching page listing; ``type``, ``plugin_id`` and ``status`` are checked by the service.

        Contract:
            Silences:
                - A bad ``limit``/``offset`` is one generic 400; an invalid ``provider_id`` means
                  all providers; ``q`` is cut to 200 characters. Clamping of ``limit``/``offset``
                  is left to ``ManualMatchService.overview``.
        """
        args = request.args
        pid = args.get("provider_id", "")
        kind = args.get("type", "")
        try:
            limit = parse_int(args.get("limit"), "limit", 50)
            offset = parse_int(args.get("offset"), "offset", 0)
        except InputError:
            return error("limit and offset must be numbers")
        try:
            overview = ctx.manual_matching.overview(
                provider_id=optional_id(pid), item_kind=None if kind in ("", "all") else kind,
                plugin_id=args.get("plugin_id") or None, status=args.get("status") or "unmatched",
                title=args.get("q", "")[:200], limit=limit, offset=offset)
        except ManualMatchError as exc:
            return manual_error(exc)
        return jsonify(overview)

    @api.post("/metadata/manual-match")
    def manual_match() -> ResponseReturnValue:
        """``confirm: false`` verifies the ID and shows what it points to; ``true`` stores it."""
        data = body()
        confirm = as_bool(data.get("confirm"))
        try:
            result = ctx.manual_matching.assign(data) if confirm else {"verified": ctx.manual_matching.preview(data)}
        except ManualMatchError as exc:
            return manual_error(exc)
        return jsonify(result)

    @api.post("/metadata/manual-match/remove")
    def remove_manual_match() -> ResponseReturnValue:
        data = body()
        try:
            removed = ctx.manual_matching.remove(data)
        except ManualMatchError as exc:
            return manual_error(exc)
        return jsonify(removed)
