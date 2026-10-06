"""Provider and category routes. Provider passwords are write-only: never returned."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional, TypedDict
from urllib.parse import urlsplit

from flask import Blueprint, jsonify

from ..jobs.manager import BusyError
from ..storage.categories import CONTENT_TYPES, MOVIE, SERIES
from ..storage.providers import Provider, ProviderValidationError
from ..storage.summary import invalidate_summary
from ..utils.redact import redact
from ..xtream.client import XtreamError
from ..xtream.urls import normalize_base_url
from .api_common import ApiDeps, as_bool, body, error, optional_id

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

log = logging.getLogger("xtreamscraper.web.api")

_DEFAULT_PORTS = {"http": 80, "https": 443}
NEW_SERVER_NEEDS_PASSWORD = "Enter the password again: the saved password is never sent to a different server"
INVALID_BASE_URL = "Base URL is not a valid URL"  # the text ProviderValidationError gives on create


class ConnectionTestResult(TypedDict):
    """The JSON of a provider connection test (also stored by ``record_test``)."""

    ok: bool
    message: str


def parse_base_url(base_url: str) -> str:
    """:func:`normalize_base_url`, with a URL ``urlsplit`` rejects raised as :class:`ProviderValidationError`."""
    try:
        return normalize_base_url(base_url)
    except ValueError:  # e.g. an unbalanced IPv6 bracket
        raise ProviderValidationError(INVALID_BASE_URL) from None


def server_origin(base_url: str) -> tuple[str, str, Optional[int]]:
    """``(scheme, host, port)`` of a provider URL; the default port is filled in.

    Contract:
        Raises:
            ProviderValidationError: For a URL ``urlsplit`` rejects (via :func:`parse_base_url`).
        Silences:
            - A bad port (``:abc``, ``:99999``) gives ``port=None`` and the raw netloc as host, so
              it only equals itself: never the stored server.
    """
    parts = urlsplit(parse_base_url(base_url))
    try:
        port: Optional[int] = parts.port or _DEFAULT_PORTS.get(parts.scheme)
        host = (parts.hostname or "").rstrip(".")
    except ValueError:  # malformed port or host: compare the raw text, which only equals itself
        port, host = None, parts.netloc.lower()
    return parts.scheme, host, port


def same_server(stored_url: str, new_url: Any) -> bool:
    """True when ``new_url`` is blank (unchanged) or points at the stored URL's scheme, host and port.

    Contract:
        Preconditions:
            - ``stored_url`` is a saved provider URL (normalised and parseable).
        Raises:
            ProviderValidationError: When ``new_url`` cannot be parsed as a URL.
    """
    if new_url is None or not str(new_url).strip():
        return True
    return server_origin(stored_url) == server_origin(str(new_url))


def register(api: Blueprint, deps: ApiDeps) -> None:
    ctx = deps.ctx

    @api.get("/providers")
    def list_providers() -> ResponseReturnValue:
        return jsonify(providers=[deps.provider_view(p) for p in ctx.providers.list()])

    @api.post("/providers")
    def create_provider() -> ResponseReturnValue:
        try:
            provider = ctx.providers.create(body())
        except ProviderValidationError as exc:
            return error(str(exc))
        invalidate_summary()
        log.info("Provider added: %s (id %d)", provider.name, provider.id)
        return jsonify(provider=deps.provider_view(provider)), 201

    @api.get("/providers/<int:provider_id>")
    def get_provider(provider_id: int) -> ResponseReturnValue:
        return jsonify(provider=deps.provider_view(deps.provider_or_404(provider_id)))

    @api.put("/providers/<int:provider_id>")
    def update_provider(provider_id: int) -> ResponseReturnValue:
        """Save provider fields; a blank password keeps the stored one only for the same server.

        Contract:
            Raises:
                NotFound: Unknown ``provider_id`` (404 through ``on_error``).
            Silences:
                - A blank password with a URL for another server, an unparseable URL or invalid
                  fields answer 400; a job holding the provider answers 409. Nothing is saved
                  and nothing is contacted in those cases.
        """
        stored = deps.provider_or_404(provider_id)
        data = body()
        # "Blank password = keep the stored one" must not hand the stored password to another server.
        try:
            if not data.get("password") and not same_server(stored.base_url, data.get("base_url")):
                return error(NEW_SERVER_NEEDS_PASSWORD)
        except ProviderValidationError as exc:
            return error(str(exc))
        try:
            with ctx.jobs.provider_locked(provider_id, "saving provider"):
                provider = ctx.providers.update(provider_id, data)
        except BusyError as exc:
            return error(str(exc), 409)
        except ProviderValidationError as exc:
            return error(str(exc))
        log.info("Provider updated: %s (id %d)", provider.name, provider.id)
        return jsonify(provider=deps.provider_view(provider))

    @api.delete("/providers/<int:provider_id>")
    def delete_provider(provider_id: int) -> ResponseReturnValue:
        provider = deps.provider_or_404(provider_id)
        try:
            with ctx.jobs.provider_locked(provider_id, "deleting provider"):
                ctx.providers.delete(provider_id)
        except BusyError:
            return error("A job is running for this provider; cancel it first", 409)
        invalidate_summary()
        log.info("Provider deleted: %s (id %d); library files were left on disk", provider.name, provider_id)
        return jsonify(deleted=True)

    @api.post("/providers/<int:provider_id>/enabled")
    def set_enabled(provider_id: int) -> ResponseReturnValue:
        deps.provider_or_404(provider_id)
        ctx.providers.set_enabled(provider_id, as_bool(body().get("enabled"), True))
        return jsonify(provider=deps.provider_view(deps.provider_or_404(provider_id)))

    def run_test(provider: Provider) -> ConnectionTestResult:
        """Authenticate against the provider with the quick client limits.

        Contract:
            Silences:
                - Every failure is ``ok: False``: ``XtreamError`` with its message, anything
                  else logged with its traceback as "Unexpected error". Messages are redacted
                  with the candidate's own username and password, which may not be registered
                  as secrets yet.
        """
        try:
            with deps.make_client(provider, quick=True) as client:
                account = client.authenticate()
        except XtreamError as exc:
            return {"ok": False, "message": redact(str(exc), (provider.username, provider.password))}
        except Exception as exc:
            log.exception("Connection test failed")
            return {"ok": False, "message": redact(f"Unexpected error: {exc}", (provider.username, provider.password))}
        return {"ok": account.authenticated, "message": account.summary()}

    @api.post("/providers/test")
    def test_unsaved() -> ResponseReturnValue:
        """Test credentials from the edit form before saving.

        A blank password means the stored one, but only while the server (scheme, host and port)
        is unchanged: the stored password is never sent anywhere else.
        """
        data = body()
        stored_id = optional_id(data.get("id"))
        stored = ctx.providers.get(stored_id) if stored_id is not None else None
        try:
            if stored and not data.get("password") and not same_server(stored.base_url, data.get("base_url")):
                return error(NEW_SERVER_NEEDS_PASSWORD)
            base_url = parse_base_url(str(data.get("base_url") or (stored.base_url if stored else "")))
        except ProviderValidationError as exc:
            return error(str(exc))
        candidate = Provider(
            id=stored.id if stored else 0,
            name=str(data.get("name") or (stored.name if stored else "test")),
            base_url=base_url,
            username=str(data.get("username") or (stored.username if stored else "")),
            password=str(data.get("password") or (stored.password if stored else "")),
            user_agent=str(data.get("user_agent") if data.get("user_agent") is not None
                           else (stored.user_agent if stored else "")),
        )
        if not candidate.base_url or not candidate.username or not candidate.password:
            return error("Base URL, username and password are required to test")
        return jsonify(run_test(candidate))

    @api.post("/providers/<int:provider_id>/test")
    def test_provider(provider_id: int) -> ResponseReturnValue:
        provider = deps.provider_or_404(provider_id)
        result = run_test(provider)
        ctx.providers.record_test(provider_id, result["ok"], result["message"])
        level = logging.INFO if result["ok"] else logging.WARNING
        log.log(level, "Connection test for %s: %s", provider.name, result["message"])
        return jsonify(result)

    # -- categories ----------------------------------------------------------------------------------
    def categories_payload(provider: Provider) -> dict[str, Any]:
        return dict(
            refreshed_at=provider.categories_refreshed_at,
            movie=[c.public_dict() for c in ctx.categories.list(provider.id, MOVIE)],
            series=[c.public_dict() for c in ctx.categories.list(provider.id, SERIES)],
        )

    @api.get("/providers/<int:provider_id>/categories")
    def get_categories(provider_id: int) -> ResponseReturnValue:
        return jsonify(categories_payload(deps.provider_or_404(provider_id)))

    @api.post("/providers/<int:provider_id>/categories/refresh")
    def refresh_categories(provider_id: int) -> ResponseReturnValue:
        provider = deps.provider_or_404(provider_id)
        try:
            with ctx.jobs.provider_locked(provider_id, "refreshing categories"):
                return refresh_locked(provider)
        except BusyError as exc:
            return error(str(exc), 409)

    def refresh_locked(provider: Provider) -> ResponseReturnValue:
        """Fetch and store the provider's categories; the caller holds the provider lock.

        Contract:
            Silences:
                - An unknown ``content_type`` in the body refreshes both types.
                - A failed type is reported in ``errors`` (redacted) and keeps its stored
                  categories; the answer is 502 only when every requested type failed, and
                  ``categories_refreshed_at`` is updated when at least one succeeded. Only the
                  first 50 parse warnings are returned.
        """
        provider_id = provider.id
        wanted = body().get("content_type")
        types = [wanted] if wanted in CONTENT_TYPES else list(CONTENT_TYPES)
        errors: list[str] = []
        warnings: list[str] = []
        with deps.make_client(provider, quick=True) as client:
            for content_type in types:
                fetch = client.get_vod_categories if content_type == MOVIE else client.get_series_categories
                try:
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
        result = categories_payload(deps.provider_or_404(provider_id))
        result.update(errors=[redact(e) for e in errors], warnings=[redact(w) for w in warnings[:50]])
        return jsonify(result), (502 if len(errors) == len(types) else 200)

    @api.put("/providers/<int:provider_id>/categories")
    def save_categories(provider_id: int) -> ResponseReturnValue:
        """Replace the selected categories of each content type present in the body.

        Contract:
            Silences:
                - Items are compared as ``str(item)``: IDs the provider never listed, and odd
                  JSON values (``true``, ``1.0``, objects), select nothing and are not reported.
                - A content type absent from the body keeps its selection.
        """
        provider = deps.provider_or_404(provider_id)
        data = body()
        for content_type in CONTENT_TYPES:
            if content_type in data and not isinstance(data[content_type], list):
                return error(f"'{content_type}' must be a list of category IDs")
        try:
            with ctx.jobs.provider_locked(provider_id, "saving categories"):
                for content_type in CONTENT_TYPES:
                    if content_type in data:
                        ctx.categories.set_selection(provider_id, content_type, [str(i) for i in data[content_type]])
        except BusyError as exc:
            return error(str(exc), 409)
        return jsonify(categories_payload(provider))
