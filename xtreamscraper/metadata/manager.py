"""Scraper manager: installed plugins + their persisted enabled/overwrite/priority state,
configuration routing (with secrets never sent back to the browser) and connection tests.
"""

from __future__ import annotations

import builtins
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from ..storage.db import Database
from ..storage.scrapers import PluginState, ScraperStateRepository
from ..utils.hashing import fingerprint
from ..utils.redact import redact, register_secret
from .plugin import MEDIA_CAPABILITIES, ConfigError, ConfigField, FieldType, ScraperPlugin, validate_against_schema
from .registry import PluginRegistry

log = logging.getLogger(__name__)

#: A non-secret field whose key ends like this (or whose value contains ``://``) names a server.
ENDPOINT_KEY_SUFFIXES = ("url", "uri", "host", "server", "endpoint", "domain")


def _normalized(f: ConfigField, value: Any) -> Any:
    """``value`` cleaned like a save would clean it, for change detection.

    Contract:
        Silences:
            - ``ConfigError``: returns a fresh ``object()``, which equals nothing, so an invalid
              value always counts as changed.
    """
    try:
        return validate_against_schema([f], {f.key: value})[f.key]
    except ConfigError:
        return object()  # an invalid value never equals anything: it counts as a change


def is_endpoint_field(f: ConfigField, *values: Any) -> bool:
    return f.key.lower().endswith(ENDPOINT_KEY_SUFFIXES) or any("://" in str(v) for v in values if v)


@dataclass
class ActivePlugin:
    plugin: ScraperPlugin
    state: PluginState
    config: dict[str, Any]
    config_fingerprint: str


class ScraperManager:
    def __init__(self, db: Database, registry: PluginRegistry) -> None:
        self.registry = registry
        self.state = ScraperStateRepository(db)
        self.sync_installed()

    def sync_installed(self) -> None:
        plugins = self.registry.all()
        self.state.ensure_installed(p.plugin_id for p in plugins)
        for plugin in plugins:
            self._register_secrets(plugin, self.state.get_config(plugin.plugin_id))

    def _register_secrets(self, plugin: ScraperPlugin, config: dict[str, Any]) -> None:
        for f in plugin.config_schema():
            if f.type is FieldType.SECRET:
                register_secret(config.get(f.key))

    def is_installed(self, plugin_id: str) -> bool:
        return self.registry.get(plugin_id) is not None

    def test_keys(self, plugin_id: str) -> set[str]:
        """Keys of the plugin's extra tests (``KeyError`` when the plugin is not installed)."""
        return {t.key for t in self._plugin(plugin_id).extra_tests()}

    def _plugin(self, plugin_id: str) -> ScraperPlugin:
        plugin = self.registry.get(plugin_id)
        if plugin is None:
            raise KeyError(plugin_id)
        return plugin

    def _ordered(self) -> list[tuple[ScraperPlugin, PluginState]]:
        self.sync_installed()
        result = []
        for state in self.state.states():
            plugin = self.registry.get(state.plugin_id)
            if plugin is not None:  # rows of uninstalled plugins are kept but ignored
                result.append((plugin, state))
        return result

    # -- listing ---------------------------------------------------------------------------------
    def list(self) -> builtins.list[dict[str, Any]]:
        items = []
        for position, (plugin, state) in enumerate(self._ordered(), start=1):
            config = self.state.get_config(plugin.plugin_id)
            items.append({
                "plugin_id": plugin.plugin_id,
                "name": plugin.display_name or plugin.plugin_id,
                "version": plugin.version or None,
                "enabled": state.enabled,
                "overwrite": state.overwrite,
                "priority": position,
                "configured": plugin.is_configured(config),
                "has_config": bool(plugin.config_schema()),
                "media_types": [c.value for c in MEDIA_CAPABILITIES if plugin.supports(c)],
                "capabilities": sorted(c.value for c in plugin.capabilities),
                "attribution": ({"text": plugin.attribution.text, "url": plugin.attribution.url}
                                if plugin.attribution else None),
                "status": self.status(plugin.plugin_id),
            })
        return items

    # -- manager settings --------------------------------------------------------------------------
    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        self._plugin(plugin_id)
        self.state.set_enabled(plugin_id, enabled)

    def set_overwrite(self, plugin_id: str, overwrite: bool) -> None:
        self._plugin(plugin_id)
        self.state.set_overwrite(plugin_id, overwrite)

    def move(self, plugin_id: str, direction: str) -> None:
        """Swap the plugin with its neighbour in the priority order.

        Contract:
            Preconditions:
                - ``direction`` is ``"up"`` or ``"down"``; anything else silently means down
                  (the web layer validates it).
            Raises:
                KeyError: The plugin is not installed (explicit).
            Silences:
                - Moving the first plugin up or the last one down is a no-op.
        """
        order = [p.plugin_id for p, _ in self._ordered()]
        if plugin_id not in order:
            raise KeyError(plugin_id)
        index = order.index(plugin_id)
        target = index - 1 if direction == "up" else index + 1
        if 0 <= target < len(order):
            order[index], order[target] = order[target], order[index]
        self.state.reorder(order)

    # -- plugin configuration -----------------------------------------------------------------------
    def public_config(self, plugin_id: str) -> dict[str, Any]:
        """Schema and current values for the plugin's page. Secrets are reported only as set/unset."""
        plugin = self._plugin(plugin_id)
        config = self.state.get_config(plugin_id)
        schema = plugin.config_schema()
        values, secrets = {}, {}
        for f in schema:
            if f.type is FieldType.SECRET:
                secrets[f.key] = bool(config.get(f.key))
            else:
                values[f.key] = config.get(f.key, f.default)
        return {
            "plugin_id": plugin_id,
            "name": plugin.display_name or plugin_id,
            "schema": [f.public_dict() for f in schema],
            "values": values,
            "secrets": secrets,
            "configured": plugin.is_configured(config),
            "tests": [{"key": t.key, "label": t.label} for t in plugin.extra_tests()],
            "status": self.status(plugin_id),
        }

    def _changed_fields(self, plugin: ScraperPlugin, current: dict[str, Any],
                        values: dict[str, Any]) -> builtins.list[ConfigField]:
        """Non-secret fields in ``values`` whose (cleaned) value differs from the stored one."""
        return [f for f in plugin.config_schema()
                if f.type is not FieldType.SECRET and f.key in values
                and _normalized(f, values[f.key]) != _normalized(f, current.get(f.key, f.default))]

    def _merged(self, plugin: ScraperPlugin, values: dict[str, Any], for_test: bool = False) -> dict[str, Any]:
        """``values`` over the stored config. A blank or missing secret keeps the stored one, unless
        that could send it somewhere new (then :class:`ConfigError` asks to enter it again):

        * save: when a server field changes (see :func:`is_endpoint_field`);
        * test: when any non-secret field changes, so an unsaved form can never point a stored
          secret at another server through a field this rule cannot recognise.

        Contract:
            Preconditions:
                - Secrets: ``{"clear": True}`` clears, a blank or missing value keeps the stored
                  one; any other value (also a non-string) is stored as given.
            Raises:
                ConfigError: A stored secret would be reused although a server field (save) or
                    any non-secret field (test) changed.
        """
        current = self.state.get_config(plugin.plugin_id)
        changed = self._changed_fields(plugin, current, values)
        if not for_test:
            changed = [f for f in changed if is_endpoint_field(f, values[f.key], current.get(f.key))]
        merged = dict(current)
        for f in plugin.config_schema():
            present = f.key in values
            value = values.get(f.key)
            if f.type is FieldType.SECRET:
                if present and value == {"clear": True}:
                    merged[f.key] = ""
                elif present and value not in (None, ""):
                    merged[f.key] = value
                elif changed and current.get(f.key):  # blank = keep the stored secret, if that is safe
                    labels = ", ".join(c.label for c in changed)
                    if for_test:
                        raise ConfigError(f"Enter the {f.label} again to test changed settings ({labels}): "
                                          f"a saved {f.label} is only reused for unchanged settings")
                    raise ConfigError(f"Enter the {f.label} again: a saved {f.label} is not reused "
                                      f"when {labels} changes")
                continue
            if present:
                merged[f.key] = value
        return merged

    def save_config(self, plugin_id: str, values: dict[str, Any]) -> dict[str, Any]:
        """Validate and store the plugin's settings; returns the public config.

        Contract:
            Raises:
                KeyError: The plugin is not installed.
                ConfigError: Invalid values, or a secret that must be entered again (see
                    :meth:`_merged`). Nothing is stored then.
        """
        plugin = self._plugin(plugin_id)
        clean = plugin.validate_config(self._merged(plugin, values))  # raises ConfigError
        self._register_secrets(plugin, clean)
        self.state.save_config(plugin_id, clean)
        log.info("Scraper plugin %s configuration saved", plugin_id)
        return self.public_config(plugin_id)

    def test(self, plugin_id: str, values: Optional[dict[str, Any]] = None,
             test: Optional[str] = None) -> tuple[bool, str]:
        """Test with the stored config, or with unsaved form values merged over it.

        ``test`` selects one of the plugin's :meth:`~.plugin.ScraperPlugin.extra_tests`
        instead of the connection test (``KeyError`` when the plugin has no such test).

        Contract:
            Raises:
                KeyError: The plugin is not installed, or has no test ``test``.
            Silences:
                - ``ConfigError`` from the merged values: ``(False, message)``.
                - Any exception from the plugin's test, and an answer that is not an
                  ``(ok, message[, status])`` sequence: ``(False, "Test failed...")``, logged.
                - A third element that is not a dict is ignored.
        """
        plugin = self._plugin(plugin_id)
        if test is not None and test not in {t.key for t in plugin.extra_tests()}:
            raise KeyError(test)
        try:
            config = plugin.validate_config(self._merged(plugin, values or {}, for_test=True))
        except ConfigError as exc:
            return False, str(exc)
        secrets = [config.get(f.key) for f in plugin.config_schema() if f.type is FieldType.SECRET]
        try:
            result = plugin.run_test(test, config) if test is not None else plugin.test_connection(config)
        except Exception as exc:  # a plugin bug must not leak a traceback with secrets
            log.warning("Scraper plugin %s test failed: %s", plugin_id, redact(exc, secrets))
            return False, redact(f"Test failed: {exc}", secrets)
        if not isinstance(result, (tuple, list)) or len(result) < 2:  # a plugin bug, see TestResult
            log.warning("Scraper plugin %s test returned no (ok, message) result: %s", plugin_id,
                        type(result).__name__)
            return False, "Test failed: the plugin returned no result"
        ok, message = result[0], result[1]
        if len(result) > 2 and isinstance(result[2], dict):
            self.record_status(plugin_id, result[2], secrets)
        return bool(ok), redact(message, secrets)

    # -- plugin status ----------------------------------------------------------------------------
    def status(self, plugin_id: str) -> dict[str, Any]:
        return self.state.get_status(plugin_id)

    def record_status(self, plugin_id: str, values: dict[str, Any], secrets: Iterable[Optional[str]] = ()) -> None:
        """Persist status entries reported by a test or a scrape job (redacted, never secrets).

        Contract:
            Preconditions:
                - A ``None`` value removes that entry; every other value is stored as redacted
                  text.
            Raises:
                sqlite3.Error: From the repository (not caught).
        """
        clean = {str(k): (None if v is None else redact(v, secrets)) for k, v in values.items()}
        if clean:
            self.state.update_status(plugin_id, clean)

    # -- scrape time ----------------------------------------------------------------------------------
    def config_fingerprint(self, plugin: ScraperPlugin, config: dict[str, Any]) -> str:
        """Changes when the plugin version or a non-secret setting changes (e.g. language)."""
        public = {f.key: config.get(f.key) for f in plugin.config_schema() if f.type is not FieldType.SECRET}
        return fingerprint(plugin.plugin_id, plugin.version, public)

    def active_plugins(self) -> tuple[builtins.list[ActivePlugin], builtins.list[str]]:
        """Enabled plugins in priority order, plus warnings for enabled-but-unconfigured ones.

        Contract:
            Silences:
                - Rows of uninstalled plugins are ignored; an enabled plugin that is not
                  configured is left out with a warning (the job runs without it).
        """
        active, warnings = [], []
        for plugin, state in self._ordered():
            if not state.enabled:
                continue
            config = self.state.get_config(plugin.plugin_id)
            if not plugin.is_configured(config):
                warnings.append(f"Scraper '{plugin.display_name or plugin.plugin_id}' is enabled but not configured; skipped")
                continue
            active.append(ActivePlugin(plugin, state, config, self.config_fingerprint(plugin, config)))
        return active, warnings

    def attributions(self) -> builtins.list[dict[str, Any]]:
        return [
            {"plugin_id": p.plugin_id, "name": p.display_name or p.plugin_id, "text": p.attribution.text,
             "url": p.attribution.url}
            for p, _ in self._ordered() if p.attribution
        ]
