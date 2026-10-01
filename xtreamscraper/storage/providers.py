"""Provider configuration persistence.

Passwords are stored in plain text in the local database (``password_scheme='plain'``).
The scheme column exists so a later version can add encryption without a breaking
schema change. The public dict form of a provider never contains the password.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ..utils.redact import register_secrets
from ..utils.timeutil import now_iso
from ..xtream.urls import normalize_base_url
from .db import Database


class ProviderValidationError(ValueError):
    pass


@dataclass
class Provider:
    id: int
    name: str
    base_url: str
    username: str
    password: str = field(repr=False)
    user_agent: str = ""
    target_folder: str = ""
    enabled: bool = True
    movies_enabled: bool = True
    series_enabled: bool = True
    created_at: str = ""
    updated_at: str = ""
    last_test_at: Optional[str] = None
    last_test_ok: Optional[bool] = None
    last_test_message: Optional[str] = None
    categories_refreshed_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Provider":
        return cls(
            id=row["id"],
            name=row["name"],
            base_url=row["base_url"],
            username=row["username"],
            password=row["password"],
            user_agent=row["user_agent"] or "",
            target_folder=row["target_folder"] or "",
            enabled=bool(row["enabled"]),
            movies_enabled=bool(row["movies_enabled"]),
            series_enabled=bool(row["series_enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_test_at=row["last_test_at"],
            last_test_ok=None if row["last_test_ok"] is None else bool(row["last_test_ok"]),
            last_test_message=row["last_test_message"],
            categories_refreshed_at=row["categories_refreshed_at"],
        )

    def public_dict(self) -> dict[str, Any]:
        """Representation for the WebUI: never includes the password."""
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "username": self.username,
            "has_password": bool(self.password),
            "user_agent": self.user_agent,
            "target_folder": self.target_folder,
            "enabled": self.enabled,
            "movies_enabled": self.movies_enabled,
            "series_enabled": self.series_enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_test_at": self.last_test_at,
            "last_test_ok": self.last_test_ok,
            "last_test_message": self.last_test_message,
            "categories_refreshed_at": self.categories_refreshed_at,
        }


_EDITABLE = (
    "name", "base_url", "username", "password", "user_agent", "target_folder",
    "enabled", "movies_enabled", "series_enabled",
)
_BOOLEAN = {"enabled", "movies_enabled", "series_enabled"}


def _clean(values: Mapping[str, Any], existing: Optional[Provider]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key in _EDITABLE:
        if key not in values:
            continue
        raw = values[key]
        if key in _BOOLEAN:
            data[key] = 1 if (raw in (True, 1) or str(raw).lower() in ("1", "true", "on", "yes")) else 0
        elif key == "password":
            # Blank password on edit means "keep the stored one".
            if raw is None or raw == "":
                continue
            data[key] = str(raw)
        else:
            data[key] = "" if raw is None else str(raw).strip()
    if "base_url" in data:
        data["base_url"] = normalize_base_url(data["base_url"])

    merged = {**(existing.__dict__ if existing else {}), **data}
    if not str(merged.get("name", "")).strip():
        raise ProviderValidationError("Provider name is required")
    if not merged.get("base_url"):
        raise ProviderValidationError("Base URL is required")
    if not str(merged.get("username", "")).strip():
        raise ProviderValidationError("Username is required")
    if not merged.get("password"):
        raise ProviderValidationError("Password is required")
    folder = str(merged.get("target_folder") or "")
    if folder and not os.path.isabs(folder):
        raise ProviderValidationError("Target folder must be an absolute path, e.g. D:\\XtreamLibrary")
    return data


class ProviderRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def register_all_secrets(self) -> None:
        """Make every stored credential known to the log redactor."""
        rows = self.db.query("SELECT username, password FROM providers")
        register_secrets(value for row in rows for value in (row["username"], row["password"]))

    def list(self) -> list[Provider]:
        return [Provider.from_row(r) for r in self.db.query("SELECT * FROM providers ORDER BY name COLLATE NOCASE, id")]

    def get(self, provider_id: int) -> Optional[Provider]:
        row = self.db.query_one("SELECT * FROM providers WHERE id = ?", (provider_id,))
        return Provider.from_row(row) if row else None

    def create(self, values: Mapping[str, Any]) -> Provider:
        data = _clean(values, None)
        now = now_iso()
        data.setdefault("enabled", 1)
        data.setdefault("movies_enabled", 1)
        data.setdefault("series_enabled", 1)
        data.update(created_at=now, updated_at=now)
        columns = ", ".join(data)
        placeholders = ", ".join("?" for _ in data)
        with self.db.transaction() as conn:
            cur = conn.execute(f"INSERT INTO providers ({columns}) VALUES ({placeholders})", tuple(data.values()))
            provider_id = cur.lastrowid
        register_secrets([data.get("username"), data.get("password")])
        provider = self.get(int(provider_id))
        assert provider is not None
        return provider

    def update(self, provider_id: int, values: Mapping[str, Any]) -> Provider:
        existing = self.get(provider_id)
        if existing is None:
            raise KeyError(provider_id)
        data = _clean(values, existing)
        if data:
            data["updated_at"] = now_iso()
            assignments = ", ".join(f"{k} = ?" for k in data)
            with self.db.transaction() as conn:
                conn.execute(f"UPDATE providers SET {assignments} WHERE id = ?", (*data.values(), provider_id))
            register_secrets([data.get("username"), data.get("password")])
        provider = self.get(provider_id)
        assert provider is not None
        return provider

    def set_enabled(self, provider_id: int, enabled: bool) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE providers SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, now_iso(), provider_id),
            )

    def record_test(self, provider_id: int, ok: bool, message: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE providers SET last_test_at = ?, last_test_ok = ?, last_test_message = ? WHERE id = ?",
                (now_iso(), 1 if ok else 0, message, provider_id),
            )

    def mark_categories_refreshed(self, provider_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE providers SET categories_refreshed_at = ? WHERE id = ?", (now_iso(), provider_id))

    def delete(self, provider_id: int) -> bool:
        """Remove the provider and its state. Library files on disk are left untouched."""
        with self.db.transaction() as conn:
            cur = conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
            return cur.rowcount > 0
