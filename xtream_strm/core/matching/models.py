"""Common candidate model and scoring profiles for the shared matcher."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class MatchCandidate:
    """One search result, converted by a plugin into provider-neutral form."""

    remote_id: str
    title: str
    alternate_titles: list[str] = field(default_factory=list)  # original / localized / alias titles
    year: Optional[int] = None
    popularity: Optional[float] = None  # tie-break for ordering only, never part of the score
    payload: Any = None  # provider-specific reference, untouched by the matcher

    def all_titles(self) -> list[str]:
        titles = [self.title, *self.alternate_titles]
        return [t for i, t in enumerate(titles) if t and t not in titles[:i]]


@dataclass(frozen=True)
class ScoringProfile:
    """How title similarity and year evidence combine into a 0–100 confidence.

    The default follows the TMDB specification; a plugin may pass its own profile.
    """

    exact_year_bonus: float = 5.0
    one_year_bonus: float = 2.0
    near_year_penalty: float = -10.0  # 2–3 years apart
    far_year_penalty: float = -25.0  # more than 3 years apart
    near_year_limit: int = 3
    ambiguity_margin: float = 2.0  # best and runner-up closer than this -> ambiguous

    def year_adjustment(self, local_year: Optional[int], candidate_year: Optional[int]) -> float:
        if local_year is None or candidate_year is None:
            return 0.0
        difference = abs(local_year - candidate_year)
        if difference == 0:
            return self.exact_year_bonus
        if difference == 1:
            return self.one_year_bonus
        if difference <= self.near_year_limit:
            return self.near_year_penalty
        return self.far_year_penalty


DEFAULT_PROFILE = ScoringProfile()


@dataclass
class ScoredCandidate:
    candidate: MatchCandidate
    title_score: float  # best similarity against any of the candidate's titles
    year_adjustment: float
    score: float  # final, clamped to 0–100
    matched_title: str  # which of the candidate's titles scored best


class Decision(str, enum.Enum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"


@dataclass
class MatchDecision:
    decision: Decision
    best: Optional[ScoredCandidate]
    ranked: list[ScoredCandidate]
    threshold: float
    reason: str = ""

    @property
    def matched(self) -> bool:
        return self.decision is Decision.MATCHED
