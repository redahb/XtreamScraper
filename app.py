"""XtreamScraper entry point.

    python app.py                 # http://localhost:6060
    python app.py --port 7070     # override the configured port for this run
    python app.py --data-dir D:\\XtreamScraper\\data
"""

from __future__ import annotations

import argparse
import logging
import sys
import webbrowser

MIN_PYTHON = (3, 10)


def main(argv: list[str] | None = None) -> int:
    """Start XtreamScraper and serve the WebUI until it is shut down; returns the exit code.

    ``argv`` defaults to ``sys.argv[1:]``. ``--host`` / ``--port`` override the stored
    ``web_host`` / ``web_port`` for this run only; ``--data-dir`` beats ``XTREAMSCRAPER_DATA_DIR``,
    which beats ``<app>/data``.

    Contract:
        Preconditions:
            - Call it once per process: it configures the root logger and builds the context,
              which marks every history row still ``running`` as interrupted.
        Raises:
            SystemExit: From ``argparse`` for unknown options or a non-integer ``--port`` (code 2),
                and for ``--help`` (code 0).
            OSError: When the data or log folder cannot be created, or when the server cannot
                bind (address in use, permission denied, invalid ``--host``); not caught. The port
                is not range-checked here, so a bad ``--port`` also fails only at bind time.
            sqlite3.Error: Opening or migrating the database (from ``build_context``).
            KeyboardInterrupt: Ctrl+C propagates out of the server loop, so the job wait and the
                database close after it are skipped (job history is fixed at the next start).
        Silences:
            - Returns 1 (with a message on stderr) on Python older than 3.10, before importing
              the app.
            - Falls back to werkzeug's development server when ``waitress`` cannot be imported;
              logged as a warning with the import error and ``sys.executable``.
            - ``webbrowser.open`` failing to find a browser is ignored by ``webbrowser`` itself.
            - Waits at most 5 seconds for running jobs after the server stops; jobs still
              running are abandoned (daemon threads) without a warning.
        Shutdown:
            - ``ctx.extra["shutdown"]`` (called by ``POST /api/shutdown``) closes every waitress
              channel through ``server.trigger.pull_trigger`` on the server's own loop. It relies
              on waitress' private ``server._map``; a waitress release that changes it breaks
              the WebUI shutdown (the API logs the error and allows a retry).
    """
    if sys.version_info < MIN_PYTHON:
        print(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required.", file=sys.stderr)
        return 1

    from xtreamscraper import APP_NAME, __version__
    from xtreamscraper.config.paths import resolve_paths
    from xtreamscraper.context import build_context
    from xtreamscraper.utils.logging_setup import set_level, setup_logging
    from xtreamscraper.web.server import WILDCARD_BINDS, browser_url, create_app, is_loopback_bind

    parser = argparse.ArgumentParser(description=f"{APP_NAME} – Xtream VOD/Series to STRM library manager")
    parser.add_argument("--data-dir", help="Folder for the database and logs (default: ./data next to app.py)")
    parser.add_argument("--host", help="Bind address for this run (default from Settings: 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Port for this run (default from Settings: 6060)")
    parser.add_argument("--open", action="store_true", help="Open the WebUI in the default browser")
    args = parser.parse_args(argv)

    paths = resolve_paths(args.data_dir)
    paths.ensure()
    log_path = setup_logging(paths.log_dir)
    log = logging.getLogger("xtreamscraper")
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
    if not is_loopback_bind(host):
        log.warning("WebUI bound to %s – it is reachable from other machines and has no login!", host)
        log.info("The WebUI answers to loopback, %s, this computer's name%s; add other host names under "
                 "Settings > Allowed host names", host, " and any IP address" if host in WILDCARD_BINDS else "")

    app = create_app(ctx)
    url = browser_url(host, port)
    log.info("WebUI available at %s", url)
    if args.open:
        webbrowser.open(url)

    try:
        from waitress import create_server
    except ImportError as exc:  # pragma: no cover - fallback for minimal installs
        log.warning(
            "waitress could not be imported (%s) by %s; using Flask's development server. "
            "Install it for this interpreter with: \"%s\" -m pip install waitress",
            exc, sys.executable, sys.executable,
        )
        from werkzeug.serving import make_server

        dev = make_server(host, port, app, threaded=True)
        ctx.extra["shutdown"] = dev.shutdown  # POST /api/shutdown: serve_forever() returns
        dev.serve_forever()
    else:
        server = create_server(app, host=host, port=port, threads=8, ident=APP_NAME)

        def stop() -> None:
            # Runs inside the server's own loop: closing every channel ends server.run().
            server.trigger.pull_trigger(lambda: [channel.close() for channel in list(server._map.values())])

        ctx.extra["shutdown"] = stop
        server.run()
        server.task_dispatcher.shutdown()
    ctx.jobs.wait_all(5)
    ctx.db.close()
    log.info("%s stopped", APP_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
