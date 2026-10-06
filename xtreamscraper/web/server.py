"""Flask application factory and request guards."""

from __future__ import annotations

import ipaddress
import logging
import mimetypes
import os
import socket
from functools import lru_cache
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_from_directory

from ..config.settings import parse_host_list
from ..context import AppContext
from ..utils.redact import redact
from .api import build_api

if TYPE_CHECKING:
    from flask import Response
    from flask.typing import ResponseReturnValue

log = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
# Windows takes MIME types from the registry, which can be wrong or missing; with "nosniff" a wrong
# type breaks the page, so the WebUI's own file types are fixed here.
for _type, _ext in (("text/css", ".css"), ("text/javascript", ".js"), ("image/svg+xml", ".svg"), ("font/woff2", ".woff2"),
                    ("image/x-icon", ".ico")):
    mimetypes.add_type(_type, _ext)

WILDCARD_BINDS = frozenset({"", "0.0.0.0", "::", "[::]"})
DEFAULT_PORTS = {"http": 80, "https": 443}


def split_host(host_header: str) -> Optional[tuple[str, Optional[int]]]:
    """``"Example.com:6060"`` -> ``("example.com", 6060)``, ``"[::1]"`` -> ``("::1", None)``; None if malformed.

    Contract:
        Silences:
            - Never raises: a blank header, one with ``/ \\ @ ? #`` or a space, an out-of-range or
              non-numeric port, unbalanced brackets or an empty host all give ``None``. The host is
              lower-cased by ``urlsplit`` (except a ``%zone`` suffix) and loses trailing dots.
    """
    value = (host_header or "").strip()
    if not value or any(c in value for c in "/\\@?# "):
        return None
    try:
        parts = urlsplit("//" + value)
        host, port = (parts.hostname or "").rstrip("."), parts.port
    except ValueError:  # bad port or bracket syntax
        return None
    return (host, port) if host else None


def _ip(value: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None


def is_loopback_bind(bind_host: str) -> bool:
    """True for ``localhost`` and any loopback address (127.0.0.0/8, ``::1``)."""
    ip = _ip(bind_host or "")
    return ip.is_loopback if ip is not None else (bind_host or "").strip().lower().rstrip(".") == "localhost"


def browser_url(bind_host: str, port: int) -> str:
    """The WebUI address to show and open for a server bound to ``bind_host``:``port``.

    ``127.0.0.1``, ``localhost`` and an IPv4 wildcard give ``localhost``; an IPv6 wildcard (``::``)
    gives the IPv6 loopback ``[::1]``; any other IPv6 literal is bracketed (a ``%zone`` suffix
    escaped as ``%25``), so ``::1`` gives ``http://[::1]:6060`` instead of the invalid
    ``http://::1:6060``. Host names and other IPv4 addresses are used as they are.
    """
    host = (bind_host or "").strip()
    if host.lower() in ("", "0.0.0.0", "127.0.0.1", "localhost"):
        return f"http://localhost:{port}"
    if host in WILDCARD_BINDS:  # the IPv6 wildcard
        return f"http://[::1]:{port}"
    if ":" in host and not host.startswith("["):
        host = "[" + host.replace("%", "%25") + "]"
    return f"http://{host}:{port}"


@lru_cache(maxsize=1)
def machine_names() -> frozenset[str]:
    """This computer's host name and fully qualified name (looked up once)."""
    names = set()
    for lookup in (socket.gethostname, socket.getfqdn):
        try:
            name = lookup().strip().lower().rstrip(".")
        except OSError:  # pragma: no cover - name lookup failure
            continue
        if name:
            names.add(name)
    return frozenset(names)


def host_allowed(host: str, bind_host: str, extra_hosts: list[str]) -> bool:
    """DNS-rebinding protection: is ``host`` (from the Host header) a name this server answers to?

    Allowed: loopback (``localhost``, 127.0.0.0/8, ``::1``), the bind address, this machine's
    name and FQDN, and the ``allowed_hosts`` setting. On a wildcard bind (``0.0.0.0``/``::``)
    any IP address is accepted too: rebinding always carries the attacker's *name* in the Host
    header, never an IP literal.

    Contract:
        Preconditions:
            - ``host`` is already lower-cased without a trailing dot (as :func:`split_host` gives
              it) and ``extra_hosts`` normalised the same way (as ``parse_host_list`` gives it);
              the name comparisons are exact, so other spellings are refused.
        Silences:
            - On a wildcard bind every IP literal passes, so an IP entry in ``extra_hosts`` has
              no effect there. An ``extra_hosts`` entry that is not an IP never matches an IP host.
    """
    bind = (bind_host or "").strip().lower()
    ip = _ip(host)
    if ip is not None:
        bind_ip = _ip(bind)
        return (ip.is_loopback or ip == bind_ip or bind in WILDCARD_BINDS
                or any(ip == _ip(h) for h in extra_hosts))
    return host == "localhost" or host == bind.rstrip(".") or host in machine_names() or host in extra_hosts


def same_origin(origin: str, scheme: str, host: str, port: Optional[int]) -> bool:
    """CSRF protection: the Origin header must match the request's scheme, host and port.

    Contract:
        Preconditions:
            - ``scheme`` lower-case and ``host`` normalised as :func:`split_host` gives it; a
              missing ``port`` means the scheme's default (80/443), on both sides.
        Silences:
            - Never raises: an unparseable Origin (bad port, unbalanced brackets) and
              ``Origin: null`` (no host) give ``False``.
    """
    try:
        parts = urlsplit(origin.strip())
        origin_host, origin_port = (parts.hostname or "").rstrip("."), parts.port
    except ValueError:
        return False
    origin_scheme = parts.scheme.lower()
    return (bool(origin_host) and origin_scheme == scheme and origin_host == host
            and (origin_port or DEFAULT_PORTS.get(origin_scheme)) == (port or DEFAULT_PORTS.get(scheme)))


def create_app(ctx: AppContext) -> Flask:
    """The WebUI app: guards, security headers, JSON errors, the ``/api`` blueprint and static files.

    Contract:
        Preconditions:
            - ``ctx.extra["bind_host"]`` should hold the address the server is bound to; without
              it the ``web_host`` setting is used for the Host check.
            - GET, HEAD and OPTIONS must never change state: ``guard`` checks only the Host header
              for them. Every other method also needs a same-origin ``Origin`` (when sent) and,
              except DELETE, a JSON body.
        Silences:
            - A request without an ``Origin`` header passes the CSRF check (non-browser clients);
              browsers always send it on cross-origin writes.
            - ``on_error`` returns an ``HTTPException``'s description as is (only constant texts
              are raised today) and turns any other exception into a redacted 500, logged with
              its traceback.
            - ``guard`` reads the settings from SQLite on every request; a read failure there
              becomes that 500.
    """
    app = Flask(__name__, static_folder=None)
    app.json.sort_keys = False  # type: ignore[attr-defined]  # the default provider is DefaultJSONProvider
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    app.extensions["xtreamscraper_ctx"] = ctx

    @app.before_request
    def guard() -> Optional[ResponseReturnValue]:
        settings = ctx.settings_store.load()
        bind_host = ctx.extra.get("bind_host") or settings.web_host
        parsed = split_host(request.host)
        if parsed is None or not host_allowed(parsed[0], bind_host, parse_host_list(settings.allowed_hosts)):
            return jsonify(error="Host not allowed; add it to 'Allowed host names' in Settings"), 403
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # CSRF protection: state changes only from this origin, only as JSON (which a
            # cross-site form cannot send without a CORS preflight we never approve).
            origin = request.headers.get("Origin")
            if origin and not same_origin(origin, request.scheme, *parsed):
                return jsonify(error="Cross-origin request refused"), 403
            if request.method != "DELETE" and not request.is_json:
                return jsonify(error="Content-Type application/json required"), 415
        return None

    @app.after_request
    def headers(response: Response) -> Response:
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
    def on_error(exc: Exception) -> ResponseReturnValue:
        from werkzeug.exceptions import HTTPException

        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code  # type: ignore[return-value]  # always set when raised
        log.exception("Unhandled API error")
        return jsonify(error=redact(f"Internal error: {exc}")), 500

    app.register_blueprint(build_api(ctx), url_prefix="/api")

    @app.get("/")
    def index() -> Response:
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return send_from_directory(STATIC_DIR, "favicon.ico")

    @app.get("/static/<path:name>")
    def static_files(name: str) -> Response:
        return send_from_directory(STATIC_DIR, name)

    return app
