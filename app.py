"""Xtream-to-STRM entry point.

    python app.py                 # http://localhost:6060
    python app.py --port 7070     # override the configured port for this run
    python app.py --data-dir D:\\XtreamSTRM\\data
"""

from __future__ import annotations

import argparse
import logging
import sys
import webbrowser

MIN_PYTHON = (3, 10)


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        print(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required.", file=sys.stderr)
        return 1

    from xtream_strm import APP_NAME, __version__
    from xtream_strm.config.paths import resolve_paths
    from xtream_strm.context import build_context
    from xtream_strm.utils.logging_setup import set_level, setup_logging
    from xtream_strm.web.server import create_app

    parser = argparse.ArgumentParser(description=f"{APP_NAME} – Xtream VOD/Series to STRM library manager")
    parser.add_argument("--data-dir", help="Folder for the database and logs (default: ./data next to app.py)")
    parser.add_argument("--host", help="Bind address for this run (default from Settings: 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Port for this run (default from Settings: 6060)")
    parser.add_argument("--open", action="store_true", help="Open the WebUI in the default browser")
    args = parser.parse_args(argv)

    paths = resolve_paths(args.data_dir)
    paths.ensure()
    log_path = setup_logging(paths.log_dir)
    log = logging.getLogger("xtream_strm")
    log.info("%s %s starting (Python %s)", APP_NAME, __version__, sys.version.split()[0])
    log.info("Data folder: %s", paths.data_dir)

    ctx = build_context(paths, log_path=log_path)
    log.info("Database ready: %s (schema v%d)", paths.db_path, ctx.db.schema_version())
    if ctx.extra.get("interrupted_jobs"):
        log.warning("%d job(s) were interrupted by the previous shutdown", ctx.extra["interrupted_jobs"])

    settings = ctx.settings_store.load()
    set_level(settings.log_level)
    host = args.host or settings.web_host
    port = args.port or settings.web_port
    ctx.extra.update(bind_host=host, bind_port=port)
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("WebUI bound to %s – it is reachable from other machines and has no login!", host)

    app = create_app(ctx)
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0', 'localhost') else host}:{port}"
    log.info("WebUI available at %s", url)
    if args.open:
        webbrowser.open(url)

    try:
        from waitress import serve
    except ImportError as exc:  # pragma: no cover - fallback for minimal installs
        log.warning(
            "waitress could not be imported (%s) by %s; using Flask's development server. "
            "Install it for this interpreter with: \"%s\" -m pip install waitress",
            exc, sys.executable, sys.executable,
        )
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    else:
        serve(app, host=host, port=port, threads=8, ident=APP_NAME)
    log.info("%s stopped", APP_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
