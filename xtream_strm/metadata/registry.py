"""Registry of available metadata providers.

Built-in providers register themselves by module import (future ``metadata/tmdb.py``,
``metadata/tvdb.py``). Third-party providers can be dropped into a ``plugins`` folder
next to the application as ``*.py`` files defining :class:`MetadataProvider` subclasses;
:func:`discover_plugins` imports them.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
import threading
from typing import Optional, Type

from .base import MetadataProvider

log = logging.getLogger(__name__)


class MetadataRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, MetadataProvider] = {}
        self._lock = threading.Lock()

    def register(self, provider: MetadataProvider | Type[MetadataProvider]) -> MetadataProvider:
        instance = provider() if inspect.isclass(provider) else provider
        if not instance.id:
            raise ValueError("Metadata provider needs a non-empty id")
        with self._lock:
            self._providers[instance.id] = instance
        log.info("Metadata provider registered: %s", instance.id)
        return instance

    def unregister(self, provider_id: str) -> None:
        with self._lock:
            self._providers.pop(provider_id, None)

    def get(self, provider_id: str) -> Optional[MetadataProvider]:
        return self._providers.get(provider_id)

    def all(self) -> list[MetadataProvider]:
        return list(self._providers.values())

    def describe(self) -> list[dict]:
        return [
            {"id": p.id, "name": p.name or p.id, "capabilities": sorted(c.value for c in p.capabilities)}
            for p in self.all()
        ]

    def discover_plugins(self, directory: str) -> int:
        """Import every ``*.py`` in ``directory`` and register provider classes found in it."""
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
                log.exception("Failed to load metadata plugin %s", filename)
                continue
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, MetadataProvider) and obj is not MetadataProvider and obj.id:
                    try:
                        self.register(obj)
                        count += 1
                    except Exception:
                        log.exception("Failed to register metadata plugin class %s", obj.__name__)
        return count


registry = MetadataRegistry()
