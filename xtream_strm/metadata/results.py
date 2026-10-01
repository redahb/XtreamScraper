"""Explicit outcomes of plugin calls.

A plugin never answers with a bare ``None``: "not found", "ambiguous", "request failed"
and "found" are different states the core handles differently (binding invalidation,
retry, continuing with the next plugin...).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .models import MetadataResult

if TYPE_CHECKING:  # pragma: no cover
    from ..core.matching.models import MatchDecision


class MatchStatus(str, enum.Enum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"
    API_ERROR = "api_error"
    UNSUPPORTED = "unsupported"


class FetchStatus(str, enum.Enum):
    OK = "ok"
    NOT_FOUND = "not_found"  # definitive: the remote item does not exist
    API_ERROR = "api_error"  # transient or configuration problem; nothing is known
    UNSUPPORTED = "unsupported"


class MatchMethod(str, enum.Enum):
    STORED_BINDING = "stored_binding"
    NFO_UNIQUEID = "nfo_uniqueid"
    EXTERNAL_ID = "external_id"
    TITLE_YEAR = "title_year"
    TITLE_ONLY = "title_only"
    PARENT = "parent"  # seasons/episodes resolved through the matched series


@dataclass
class Candidate:
    """Diagnostic information for ambiguous or rejected matches."""

    remote_id: str
    title: str
    year: Optional[int] = None
    score: Optional[float] = None


@dataclass
class MatchQuery:
    media_type: str  # "movie" | "series"
    title: str
    year: Optional[int] = None
    external_ids: dict[str, str] = field(default_factory=dict)  # IDs already known (e.g. from the NFO)


@dataclass
class MatchOutcome:
    status: MatchStatus
    remote_id: Optional[str] = None
    method: Optional[MatchMethod] = None
    score: Optional[float] = None
    matched_title: Optional[str] = None
    matched_year: Optional[int] = None
    candidates: list[Candidate] = field(default_factory=list)
    message: Optional[str] = None
    format_error: bool = False  # see FetchOutcome.format_error

    @classmethod
    def matched(cls, remote_id: str, method: MatchMethod, score: Optional[float] = None,
                title: Optional[str] = None, year: Optional[int] = None) -> "MatchOutcome":
        return cls(MatchStatus.MATCHED, str(remote_id), method, score, title, year)

    @classmethod
    def unmatched(cls, candidates: Optional[list[Candidate]] = None, message: Optional[str] = None) -> "MatchOutcome":
        return cls(MatchStatus.UNMATCHED, candidates=candidates or [], message=message)

    @classmethod
    def ambiguous(cls, candidates: list[Candidate], message: Optional[str] = None) -> "MatchOutcome":
        return cls(MatchStatus.AMBIGUOUS, candidates=candidates, message=message)

    @classmethod
    def error(cls, message: str) -> "MatchOutcome":
        return cls(MatchStatus.API_ERROR, message=message)

    @classmethod
    def provider_format_error(cls, message: str) -> "MatchOutcome":
        return cls(MatchStatus.API_ERROR, message=message, format_error=True)

    @classmethod
    def from_decision(cls, decision: "MatchDecision", method: MatchMethod, max_candidates: int = 5) -> "MatchOutcome":
        """Translate a shared-matcher decision (see ``xtream_strm.core.matching``)."""
        candidates = [Candidate(str(s.candidate.remote_id), s.matched_title, s.candidate.year, s.score)
                      for s in decision.ranked[:max_candidates]]
        if decision.matched and decision.best is not None:
            best = decision.best
            outcome = cls.matched(str(best.candidate.remote_id), method, best.score, best.matched_title,
                                  best.candidate.year)
            outcome.candidates = candidates
            return outcome
        if decision.decision.value == "ambiguous":
            return cls.ambiguous(candidates, decision.reason)
        return cls.unmatched(candidates, decision.reason)


@dataclass
class FetchOutcome:
    status: FetchStatus
    metadata: Optional[MetadataResult] = None
    remote_id: Optional[str] = None  # e.g. the remote episode ID
    message: Optional[str] = None
    #: The source answered, but in a format the plugin no longer understands (e.g. a
    #: changed page structure). Handled like an API error: the item is *not* reported as
    #: missing and its binding is kept, so a fixed plugin picks it up again.
    format_error: bool = False

    @classmethod
    def ok(cls, metadata: MetadataResult, remote_id: Optional[str] = None) -> "FetchOutcome":
        return cls(FetchStatus.OK, metadata, remote_id or metadata.remote_id)

    @classmethod
    def not_found(cls, message: Optional[str] = None) -> "FetchOutcome":
        return cls(FetchStatus.NOT_FOUND, message=message)

    @classmethod
    def error(cls, message: str) -> "FetchOutcome":
        return cls(FetchStatus.API_ERROR, message=message)

    @classmethod
    def provider_format_error(cls, message: str) -> "FetchOutcome":
        """The provider's response format changed or could not be parsed (never "not found")."""
        return cls(FetchStatus.API_ERROR, message=message, format_error=True)

    @classmethod
    def unsupported(cls) -> "FetchOutcome":
        return cls(FetchStatus.UNSUPPORTED)
