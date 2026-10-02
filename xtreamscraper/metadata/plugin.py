"""The scraper plugin interface.

A plugin only:

1. declares its capabilities,
2. declares its configuration (a schema; the WebUI renders the plugin's own page from it),
3. searches/matches remote items,
4. retrieves metadata,
5. returns normalized :class:`~.models.MetadataResult` objects,
6. declares which IDs a user may enter by hand (:meth:`ScraperPlugin.manual_id_fields`), checks
   their syntax and resolves alternate IDs to its own remote IDs.

It never writes NFO XML, never decides merge/overwrite behaviour and never stores its
own enabled/priority/overwrite state: the core owns all of that.

Per scrape job the core calls :meth:`ScraperPlugin.create_session` once; the session can
keep HTTP connections and in-memory caches (series/season details...) for that job.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from ..core.matching.models import ScoringProfile
from .results import FetchOutcome, MatchOutcome, MatchQuery, MatchStatus, ResolveOutcome


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


class ManualIdError(ValueError):
    """A manually entered ID is malformed; the message is shown to the user."""


@dataclass
class ManualIdField:
    """One kind of ID a user may enter by hand for a plugin (see
    :meth:`ScraperPlugin.manual_id_fields`). The plugin's own :attr:`~ScraperPlugin.id_namespace`
    is its native ID; any other namespace is an alternate ID the plugin resolves itself."""

    namespace: str
    label: str
    help: str = ""
    placeholder: str = ""
    pattern_hint: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {"namespace": self.namespace, "label": self.label, "help": self.help,
                "placeholder": self.placeholder, "pattern_hint": self.pattern_hint}


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
    advanced: bool = False  # shown in the page's "Advanced" section

    def public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "type": self.type.value,
            "default": None if self.type is FieldType.SECRET else self.default,
            "required": self.required, "help": self.help, "minimum": self.minimum,
            "maximum": self.maximum, "choices": [{"value": v, "label": l} for v, l in self.choices],
            "advanced": self.advanced,
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


@dataclass
class ArtworkPayload:
    """Image bytes a session fetched for an :attr:`~.models.Artwork.download_ref`.

    The core validates the payload and writes the file; the plugin never does.
    """

    data: bytes
    content_type: str = ""


@dataclass
class TestAction:
    """An additional test button on the plugin's settings page (besides Test Connection)."""

    key: str
    label: str


#: What a connection test returns: ``(ok, message)`` or ``(ok, message, status)``. ``status``
#: entries are merged into the plugin's persisted status (see :meth:`ScraperSession.status_update`).
TestResult = Union[tuple[bool, str], tuple[bool, str, dict[str, Any]]]


class ScraperSession(abc.ABC):
    """Per-job worker. Default implementations report the operation as unsupported."""

    #: Set by a session that must not be called again during this job (e.g. the source's
    #: request quota is exhausted or its credentials were rejected). The message is safe
    #: to show. The core then skips the plugin for the remaining items, records nothing
    #: for them (so the next job tries again) and lets lower-priority plugins continue.
    suspended: Optional[str] = None

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

    def resolve_manual_id(self, media_type: str, namespace: str, value: str) -> ResolveOutcome:
        """The native remote ID of the ``"movie"`` or ``"series"`` an alternate manual ID points to.

        Called only for alternate namespaces the plugin declared in
        :meth:`ScraperPlugin.manual_id_fields`, with a value from
        :meth:`ScraperPlugin.normalize_manual_id`. It must use only that ID: an unknown ID is
        ``not_found``, never a fallback to a title search.
        """
        return ResolveOutcome.unsupported()

    def fetch_artwork(self, download_ref: str) -> Optional[ArtworkPayload]:
        """Bytes for an artwork candidate's ``download_ref``, or ``None`` when unavailable.

        Only the core artwork manager calls this, for local downloads; it falls back to the
        candidate's public ``url`` on ``None``, an exception or an invalid payload.
        Authenticated URLs built here must never be returned, logged or persisted.
        """
        return None

    def status_update(self) -> dict[str, Any]:
        """Status entries to persist when the job ends (e.g. "Last successful request").

        Values must be plain, secret-free strings; ``None`` removes an entry.
        """
        return {}

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

    def manual_id_fields(self, media_type: str) -> list[ManualIdField]:
        """The IDs a user may enter by hand to match a root ``"movie"`` or ``"series"``.

        Nothing by default. The native namespace (:attr:`id_namespace`) is fetched directly;
        every other namespace listed here is resolved by
        :meth:`ScraperSession.resolve_manual_id`.
        """
        return []

    def normalize_manual_id(self, namespace: str, value: str) -> str:
        """The clean form of a manually entered ID, or :class:`ManualIdError` when malformed.

        Only called for namespaces listed by :meth:`manual_id_fields`. The core knows nothing
        about any ID syntax.
        """
        raise ManualIdError("This ID type is not supported")

    def validate_config(self, values: dict[str, Any]) -> dict[str, Any]:
        """Return the cleaned configuration or raise :class:`ConfigError`."""
        return validate_against_schema(self.config_schema(), values)

    def is_configured(self, config: dict[str, Any]) -> bool:
        return all(config.get(f.key) not in (None, "") for f in self.config_schema() if f.required)

    def test_connection(self, config: dict[str, Any]) -> TestResult:
        """Make a lightweight authenticated request. Messages must never contain secrets."""
        return True, "Nothing to test"

    def extra_tests(self) -> list[TestAction]:
        """Further tests offered on the settings page (run through :meth:`run_test`)."""
        return []

    def run_test(self, key: str, config: dict[str, Any]) -> TestResult:
        raise KeyError(key)

    @abc.abstractmethod
    def create_session(self, config: dict[str, Any]) -> ScraperSession:
        """A session for one scrape job."""
