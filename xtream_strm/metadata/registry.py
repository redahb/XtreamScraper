"""Discovery of installed scraper plugins.

Built-in plugins are registered by the application at start-up; additional plugins can
be dropped into ``<app>/plugins/*.py``. The registry only knows which plugins exist;
their enabled/priority/overwrite state and configuration live in the database
(:mod:`.manager`).
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
import threading
from typing import Optional, Type

from .plugin import ScraperPlugin

log = logging.getLogger(__name__)


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, ScraperPlugin] = {}
        self._lock = threading.Lock()

    def register(self, plugin: ScraperPlugin | Type[ScraperPlugin]) -> ScraperPlugin:
        instance = plugin() if inspect.isclass(plugin) else plugin
        if not instance.plugin_id:
            raise ValueError("Scraper plugin needs a non-empty plugin_id")
        with self._lock:
            self._plugins[instance.plugin_id] = instance
        log.info("Scraper plugin installed: %s %s", instance.plugin_id, instance.version or "")
        return instance

    def unregister(self, plugin_id: str) -> None:
        with self._lock:
            self._plugins.pop(plugin_id, None)

    def get(self, plugin_id: str) -> Optional[ScraperPlugin]:
        return self._plugins.get(plugin_id)

    def all(self) -> list[ScraperPlugin]:
        with self._lock:
            return list(self._plugins.values())

    def discover(self, directory: str) -> int:
        """Import ``*.py`` files in ``directory`` and register the plugin classes they define."""
        if not os.path.isdir(directory):
            return 0
        count = 0
        for filename in sorted(os.listdir(directory)):
            if not filename.endswith(".py") or filename.startswith("_"):
                continue
            path = os.path.join(directory, filename)
            try:
                spec = importlib.util.spec_from_file_location(f"xtream_strm_plugin_{filename[:-3]}", path)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception:
                log.exception("Failed to load scraper plugin file %s", filename)
                continue
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, ScraperPlugin) and obj is not ScraperPlugin and obj.plugin_id \
                        and obj.__module__ == module.__name__:
                    try:
                        self.register(obj)
                        count += 1
                    except Exception:
                        log.exception("Failed to register scraper plugin %s", obj.__name__)
        return count


registry = PluginRegistry()
