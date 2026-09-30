"""Scraper plugins bundled with the application."""

from __future__ import annotations

from ..registry import PluginRegistry


def register_builtin(registry: PluginRegistry) -> None:
    from .tmdb import TMDBPlugin

    if registry.get(TMDBPlugin.plugin_id) is None:
        registry.register(TMDBPlugin)
