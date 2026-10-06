"""Scraper plugin routes: order, enabled/overwrite flags, configuration and tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Blueprint, jsonify

from ..metadata.plugin import ConfigError
from .api_common import ApiDeps, as_bool, body, error

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue


def register(api: Blueprint, deps: ApiDeps) -> None:
    ctx = deps.ctx

    def not_installed() -> ResponseReturnValue:
        return error("Scraper plugin not installed", 404)

    @api.get("/scrapers")
    def list_scrapers() -> ResponseReturnValue:
        return jsonify(plugins=ctx.scrapers.list())

    # A missing plugin is checked explicitly: a KeyError raised inside a plugin is a bug (500), not a 404.
    @api.post("/scrapers/<plugin_id>/enabled")
    def scraper_enabled(plugin_id: str) -> ResponseReturnValue:
        if not ctx.scrapers.is_installed(plugin_id):
            return not_installed()
        ctx.scrapers.set_enabled(plugin_id, as_bool(body().get("enabled"), True))
        return jsonify(plugins=ctx.scrapers.list())

    @api.post("/scrapers/<plugin_id>/overwrite")
    def scraper_overwrite(plugin_id: str) -> ResponseReturnValue:
        if not ctx.scrapers.is_installed(plugin_id):
            return not_installed()
        ctx.scrapers.set_overwrite(plugin_id, as_bool(body().get("overwrite")))
        return jsonify(plugins=ctx.scrapers.list())

    @api.post("/scrapers/<plugin_id>/move")
    def scraper_move(plugin_id: str) -> ResponseReturnValue:
        direction = body().get("direction")
        if direction not in ("up", "down"):
            return error("direction must be up or down")
        if not ctx.scrapers.is_installed(plugin_id):
            return not_installed()
        ctx.scrapers.move(plugin_id, direction)
        return jsonify(plugins=ctx.scrapers.list())

    @api.get("/scrapers/<plugin_id>/config")
    def scraper_config(plugin_id: str) -> ResponseReturnValue:
        if not ctx.scrapers.is_installed(plugin_id):
            return not_installed()
        return jsonify(ctx.scrapers.public_config(plugin_id))

    @api.put("/scrapers/<plugin_id>/config")
    def save_scraper_config(plugin_id: str) -> ResponseReturnValue:
        values = body().get("values")
        if not isinstance(values, dict):
            return error("'values' must be an object")
        if not ctx.scrapers.is_installed(plugin_id):
            return not_installed()
        try:
            saved = ctx.scrapers.save_config(plugin_id, values)
        except ConfigError as exc:
            return error(str(exc))
        return jsonify(saved)

    @api.post("/scrapers/<plugin_id>/test")
    def test_scraper(plugin_id: str) -> ResponseReturnValue:
        data = body()
        values, test = data.get("values"), data.get("test")
        if test is not None and not isinstance(test, str):
            return error("'test' must be a string")
        if not ctx.scrapers.is_installed(plugin_id):
            return not_installed()
        if test is not None and test not in ctx.scrapers.test_keys(plugin_id):
            return error("Unknown test", 404)
        ok, message = ctx.scrapers.test(plugin_id, values if isinstance(values, dict) else None, test)
        return jsonify(ok=ok, message=message, status=ctx.scrapers.status(plugin_id))
