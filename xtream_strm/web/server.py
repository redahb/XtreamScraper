"""Flask application factory and request guards."""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_from_directory

from ..context import AppContext
from ..utils.redact import redact
from .api import build_api

log = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _hostname(host_header: str) -> str:
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.split(":")[0]


def create_app(ctx: AppContext) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.json.sort_keys = False
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    app.extensions["xtream_ctx"] = ctx

    @app.before_request
    def guard():
        settings_host = ctx.extra.get("bind_host") or ctx.settings_store.load().web_host
        loopback_only = settings_host in ("127.0.0.1", "localhost", "::1")
        # DNS-rebinding protection: when bound to localhost only accept localhost Host headers.
        if loopback_only and _hostname(request.host) not in LOOPBACK_HOSTS:
            return jsonify(error="Host not allowed"), 403
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # CSRF protection: state changes only from this origin, only as JSON (which a
            # cross-site form cannot send without a CORS preflight we never approve).
            origin = request.headers.get("Origin")
            if origin and _hostname(urlsplit(origin).netloc) != _hostname(request.host):
                return jsonify(error="Cross-origin request refused"), 403
            if request.method != "DELETE" and not request.is_json:
                return jsonify(error="Content-Type application/json required"), 415
        return None

    @app.after_request
    def headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(Exception)
    def on_error(exc):
        from werkzeug.exceptions import HTTPException

        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        log.exception("Unhandled API error")
        return jsonify(error=redact(f"Internal error: {exc}")), 500

    app.register_blueprint(build_api(ctx), url_prefix="/api")

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name: str):
        return send_from_directory(STATIC_DIR, name)

    return app
