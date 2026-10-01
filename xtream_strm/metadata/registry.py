"""Discovery of installed scraper plugins.

The application ships no plugins of its own: every scraper lives in ``<app>/plugins/``,
either as a single ``*.py`` file or as a self-contained package folder
(``plugins/<id>/__init__.py`` plus its own modules, tests and README). Package folders are
imported as ``xtream_strm_plugins.<folder>``. The registry only knows which plugins exist;
their enabled/priority/overwrite state and configuration live in the database
(:mod:`.manager`). A plugin that fails to load is logged and skipped.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
import sys
import threading
import types
from typing import Optional, Type

from .plugin import ScraperPlugin

log = logging.getLogger(__name__)

PACKAGE = "xtream_strm_plugins"
"""Parent package under which plugin folders are imported."""


def add_plugin_path(directory: str) -> None:
    """Make the plugin folders in ``directory`` importable as ``xtream_strm_plugins.<folder>``."""
    namespace = sys.modules.get(PACKAGE)
    if namespace is None:
        namespace = types.ModuleType(PACKAGE)
        namespace.__path__ = []
        sys.modules[PACKAGE] = namespace
    directory = os.path.abspath(directory)
    if directory not in namespace.__path__:
        namespace.__path__.append(directory)


def _same_file(a: Optional[str], b: str) -> bool:
    return bool(a) and os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


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
        """Load every plugin file and plugin folder in ``directory``; returns how many plugins registered."""
        if not os.path.isdir(directory):
            return 0
        count = 0
        for name in sorted(os.listdir(directory)):
            if name.startswith(("_", ".")):
                continue
            path = os.path.join(directory, name)
            if name.endswith(".py") and os.path.isfile(path):
                count += self.load_file(path)
            elif os.path.isfile(os.path.join(path, "__init__.py")):
                count += self.load_package(path)
        return count

    def load_file(self, path: str) -> int:
        """Import a single-file plugin (``plugins/<name>.py``)."""
        filename = os.path.basename(path)
        try:
            spec = importlib.util.spec_from_file_location(f"xtream_strm_plugin_{filename[:-3]}", path)
            if spec is None or spec.loader is None:
                return 0
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            log.exception("Failed to load scraper plugin file %s", filename)
            return 0
        return self._register_from(module)

    def load_package(self, path: str) -> int:
        """Import a plugin folder (``plugins/<name>/__init__.py``) as ``xtream_strm_plugins.<name>``."""
        path = os.path.abspath(path)
        name = os.path.basename(path)
        if not name.isidentifier():
            log.warning("Skipped scraper plugin folder %s: the folder name is not a valid Python name", name)
            return 0
        module_name = f"{PACKAGE}.{name}"
        init = os.path.join(path, "__init__.py")
        module = sys.modules.get(module_name)
        if module is not None and not _same_file(getattr(module, "__file__", None), init):
            log.warning("Skipped scraper plugin folder %s: a plugin folder with that name is already loaded", path)
            return 0
        if module is None:
            add_plugin_path(os.path.dirname(path))
            try:
                spec = importlib.util.spec_from_file_location(module_name, init, submodule_search_locations=[path])
                if spec is None or spec.loader is None:
                    return 0
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
            except Exception:
                for loaded in [m for m in sys.modules if m == module_name or m.startswith(module_name + ".")]:
                    sys.modules.pop(loaded, None)
                log.exception("Failed to load scraper plugin folder %s", name)
                return 0
        return self._register_from(module)

    def _register_from(self, module: types.ModuleType) -> int:
        """Register the plugin classes a plugin module or package defines (not the ones it imports)."""
        prefix = module.__name__ + "."
        count, seen = 0, set()
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj in seen or not issubclass(obj, ScraperPlugin) or obj is ScraperPlugin or not obj.plugin_id:
                continue
            if obj.__module__ != module.__name__ and not obj.__module__.startswith(prefix):
                continue
            seen.add(obj)
            try:
                self.register(obj)
                count += 1
            except Exception:
                log.exception("Failed to register scraper plugin %s", obj.__name__)
        return count
