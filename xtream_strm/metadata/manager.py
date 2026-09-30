"""Scraper manager: installed plugins + their persisted enabled/overwrite/priority state,
configuration routing (with secrets never sent back to the browser) and connection tests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from ..storage.db import Database
from ..storage.scrapers import PluginState, ScraperStateRepository
from ..utils.hashing import fingerprint
from ..utils.redact import redact, register_secret
from .plugin import MEDIA_CAPABILITIES, ConfigError, FieldType, ScraperPlugin
from .registry import PluginRegistry

log = logging.getLogger(__name__)


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
    def list(self) -> list[dict[str, Any]]:
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
        }

    def _merged(self, plugin: ScraperPlugin, values: dict[str, Any]) -> dict[str, Any]:
        current = self.state.get_config(plugin.plugin_id)
        merged = dict(current)
        for f in plugin.config_schema():
            if f.key not in values:
                continue
            value = values[f.key]
            if f.type is FieldType.SECRET:
                if value in (None, ""):
                    continue  # blank = keep the stored secret
                if value == {"clear": True}:
                    merged[f.key] = ""
                    continue
            merged[f.key] = value
        return merged

    def save_config(self, plugin_id: str, values: dict[str, Any]) -> dict[str, Any]:
        plugin = self._plugin(plugin_id)
        clean = plugin.validate_config(self._merged(plugin, values))  # raises ConfigError
        self._register_secrets(plugin, clean)
        self.state.save_config(plugin_id, clean)
        log.info("Scraper plugin %s configuration saved", plugin_id)
        return self.public_config(plugin_id)

    def test(self, plugin_id: str, values: Optional[dict[str, Any]] = None) -> tuple[bool, str]:
        """Test with the stored config, or with unsaved form values merged over it."""
        plugin = self._plugin(plugin_id)
        try:
            config = plugin.validate_config(self._merged(plugin, values or {}))
        except ConfigError as exc:
            return False, str(exc)
        secrets = [config.get(f.key) for f in plugin.config_schema() if f.type is FieldType.SECRET]
        try:
            ok, message = plugin.test_connection(config)
        except Exception as exc:  # a plugin bug must not leak a traceback with secrets
            log.warning("Scraper plugin %s test failed: %s", plugin_id, redact(exc, secrets))
            return False, redact(f"Test failed: {exc}", secrets)
        return bool(ok), redact(message, secrets)

    # -- scrape time ----------------------------------------------------------------------------------
    def config_fingerprint(self, plugin: ScraperPlugin, config: dict[str, Any]) -> str:
        """Changes when the plugin version or a non-secret setting changes (e.g. language)."""
        public = {f.key: config.get(f.key) for f in plugin.config_schema() if f.type is not FieldType.SECRET}
        return fingerprint(plugin.plugin_id, plugin.version, public)

    def active_plugins(self) -> tuple[list[ActivePlugin], list[str]]:
        """Enabled plugins in priority order, plus warnings for enabled-but-unconfigured ones."""
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

    def attributions(self) -> list[dict[str, Any]]:
        return [
            {"plugin_id": p.plugin_id, "name": p.display_name or p.plugin_id, "text": p.attribution.text,
             "url": p.attribution.url}
            for p, _ in self._ordered() if p.attribution
        ]
