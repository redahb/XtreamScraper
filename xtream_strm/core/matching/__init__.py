"""Shared title matching for metadata scraper plugins.

Plugins turn their search results into :class:`MatchCandidate` objects; the shared
:class:`TitleMatcher` scores, ranks and decides. Plugins do not implement their own fuzzy
matching unless they have a genuinely provider-specific need.
"""

from .matcher import TitleMatcher, match_candidates
from .models import DEFAULT_PROFILE, MatchCandidate, MatchDecision, ScoredCandidate, ScoringProfile
from .normalization import normalize_title

__all__ = [
    "DEFAULT_PROFILE", "MatchCandidate", "MatchDecision", "ScoredCandidate", "ScoringProfile",
    "TitleMatcher", "match_candidates", "normalize_title",
]
