"""JSON API used by the WebUI.

The routes are grouped per area in ``api_<area>.py`` modules that share one
:class:`~.api_common.ApiDeps`; all of them register on the single ``api`` blueprint, so
endpoint names stay ``api.<function>``. Provider passwords and scraper secrets are
write-only: they can be set but are never returned.
"""

from __future__ import annotations

from flask import Blueprint

from ..context import AppContext
from . import api_jobs, api_matching, api_providers, api_scrapers, api_system
from .api_common import ApiDeps, InputError, error
from .api_system import SHUTDOWN_JOB_WAIT

__all__ = ["SHUTDOWN_JOB_WAIT", "build_api"]

AREAS = (api_system, api_providers, api_jobs, api_scrapers, api_matching)


def build_api(ctx: AppContext) -> Blueprint:
    api = Blueprint("api", __name__)
    deps = ApiDeps(ctx)
    api.register_error_handler(InputError, lambda exc: error(str(exc)))
    for area in AREAS:
        area.register(api, deps)
    return api
