"""The scraper plugin interface.

A plugin only:

1. declares its capabilities,
2. declares its configuration (a schema; the WebUI renders the plugin's own page from it),
3. searches/matches remote items,
4. retrieves metadata,
5. returns normalized :class:`~.models.MetadataResult` objects.

It never writes NFO XML, never decides merge/overwrite behaviour and never stores its
own enabled/priority/overwrite state: the core owns all of that.

Per scrape job the core calls :meth:`ScraperPlugin.create_session` once; the session can
keep HTTP connections and in-memory caches (series/season details...) for that job.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from typing import Any, Optional

from ..core.matching.models import ScoringProfile
from .results import FetchOutcome, MatchOutcome, MatchQuery, MatchStatus


class Capability(str, enum.Enum):
    MOVIES = "movies"
    SERIES = "series"
    SEASONS = "seasons"
    EPISODES = "episodes"
    ARTWORK = "artwork"
    EXTERNAL_IDS = "external_ids"
    RATINGS = "ratings"


MEDIA_CAPABILITIES = (Capability.MOVIES, Capability.SERIES, Capability.SEASONS, Capability.EPISODES)


class ConfigError(ValueError):
    """Invalid plugin configuration; the message is shown to the user."""


class FieldType(str, enum.Enum):
    STRING = "string"
    SECRET = "secret"  # never returned to the browser once saved
    INTEGER = "integer"
    BOOLEAN = "boolean"
    SELECT = "select"


@dataclass
class ConfigField:
    key: str
    label: str
    type: FieldType = FieldType.STRING
    default: Any = None
    required: bool = False
    help: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: list[tuple[str, str]] = field(default_factory=list)  # (value, label)
    pattern_hint: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "type": self.type.value,
            "default": None if self.type is FieldType.SECRET else self.default,
            "required": self.required, "help": self.help, "minimum": self.minimum,
            "maximum": self.maximum, "choices": [{"value": v, "label": l} for v, l in self.choices],
        }


@dataclass
class Attribution:
    """Credits a plugin's data source requires (shown on the About page)."""

    text: str
    url: Optional[str] = None


def validate_against_schema(schema: list[ConfigField], values: dict[str, Any]) -> dict[str, Any]:
    """Type-check and bound-check ``values``; returns a complete, cleaned config dict."""
    clean: dict[str, Any] = {}
    for f in schema:
        raw = values.get(f.key, f.default)
        if f.type is FieldType.BOOLEAN:
            value: Any = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
        elif f.type is FieldType.INTEGER:
            if raw in (None, ""):
                value = None
            else:
                try:
                    value = int(str(raw).strip())
                except ValueError:
                    raise ConfigError(f"{f.label} must be a whole number") from None
                if f.minimum is not None and value < f.minimum or f.maximum is not None and value > f.maximum:
                    raise ConfigError(f"{f.label} must be between {f.minimum:g} and {f.maximum:g}")
        else:
            value = "" if raw is None else str(raw).strip()
            if f.type is FieldType.SELECT and value and f.choices and value not in {c for c, _ in f.choices}:
                raise ConfigError(f"{f.label}: invalid choice")
        # Required fields are not enforced here: a partially filled page can be saved and the
        # plugin is then reported as "not configured" (see ScraperPlugin.is_configured).
        clean[f.key] = value
    return clean


class ScraperSession(abc.ABC):
    """Per-job worker. Default implementations report the operation as unsupported."""

    def match_movie(self, query: MatchQuery) -> MatchOutcome:
        return MatchOutcome(MatchStatus.UNSUPPORTED)

    def get_movie(self, remote_id: str) -> FetchOutcome:
        return FetchOutcome.unsupported()

    def match_series(self, query: MatchQuery) -> MatchOutcome:
        return MatchOutcome(MatchStatus.UNSUPPORTED)

    def get_series(self, remote_id: str) -> FetchOutcome:
        return FetchOutcome.unsupported()

    def get_season(self, series_remote_id: str, season_number: int) -> FetchOutcome:
        return FetchOutcome.unsupported()

    def get_episode(self, series_remote_id: str, season_number: int, episode_number: int) -> FetchOutcome:
        return FetchOutcome.unsupported()

    def close(self) -> None:
        """Release connections/caches at the end of the job."""


class ScraperPlugin(abc.ABC):
    #: Stable identifier, used for persisted state and bindings.
    plugin_id: str = ""
    display_name: str = ""
    version: str = ""
    capabilities: frozenset[Capability] = frozenset()
    #: External-ID namespace this plugin's remote IDs belong to (``<uniqueid type=...>``).
    id_namespace: str = ""
    attribution: Optional[Attribution] = None
    #: Optional override of the shared matcher's scoring (None = core default profile).
    scoring_profile: Optional[ScoringProfile] = None

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def config_schema(self) -> list[ConfigField]:
        return []

    def validate_config(self, values: dict[str, Any]) -> dict[str, Any]:
        """Return the cleaned configuration or raise :class:`ConfigError`."""
        return validate_against_schema(self.config_schema(), values)

    def is_configured(self, config: dict[str, Any]) -> bool:
        return all(config.get(f.key) not in (None, "") for f in self.config_schema() if f.required)

    def test_connection(self, config: dict[str, Any]) -> tuple[bool, str]:
        """Make a lightweight authenticated request. Messages must never contain secrets."""
        return True, "Nothing to test"

    @abc.abstractmethod
    def create_session(self, config: dict[str, Any]) -> ScraperSession:
        """A session for one scrape job."""
