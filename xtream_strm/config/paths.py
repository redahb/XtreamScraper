"""Resolution of the application's data, database and log locations."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

DATA_DIR_ENV = "XTREAM_STRM_DATA_DIR"


def app_root() -> str:
    """Directory containing ``app.py`` (or the frozen executable)."""
    if getattr(sys, "frozen", False):  # pragma: no cover - PyInstaller-style bundles
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass(frozen=True)
class AppPaths:
    root: str
    data_dir: str
    plugins: Optional[str] = None  # scraper plugin folder; defaults to <app>/plugins

    @property
    def plugins_dir(self) -> str:
        return self.plugins or os.path.join(self.root, "plugins")

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "xtream-strm.db")

    @property
    def log_dir(self) -> str:
        return os.path.join(self.data_dir, "logs")

    def ensure(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)


def resolve_paths(data_dir: Optional[str] = None, plugins_dir: Optional[str] = None) -> AppPaths:
    """Data directory priority: explicit argument > environment variable > ``<app>/data``."""
    root = app_root()
    chosen = data_dir or os.environ.get(DATA_DIR_ENV) or os.path.join(root, "data")
    return AppPaths(root=root, data_dir=os.path.abspath(chosen),
                    plugins=os.path.abspath(plugins_dir) if plugins_dir else None)
