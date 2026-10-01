"""The shared title/year matcher.

score = best RapidFuzz similarity of the local title against the candidate's titles
        + year adjustment from the scoring profile, clamped to 0–100

Decision:

* no candidate reaches the threshold -> unmatched
* the best two candidates are closer than the profile's ambiguity margin -> ambiguous
  (never guessed; year evidence already separates remakes through the score)
* otherwise -> matched

Ordering is fully deterministic: score, title score, year evidence, popularity (only as a
final tie-break) and finally the remote ID.
"""

from __future__ import annotations

from typing import Iterable, Optional

from rapidfuzz import fuzz

from .models import (
    DEFAULT_PROFILE,
    Decision,
    MatchCandidate,
    MatchDecision,
    ScoredCandidate,
    ScoringProfile,
)
from .normalization import normalize_title


def title_similarity(a: str, b: str) -> float:
    """0–100 similarity of two normalized titles.

    ``ratio`` catches small spelling differences; ``token_sort_ratio`` tolerates a changed
    word order ("Matrix, The"). Partial-match scorers are deliberately not used: they would
    rate "The Matrix" as a perfect match for "The Matrix Reloaded".
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    return float(max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b)))


class TitleMatcher:
    def __init__(self, profile: Optional[ScoringProfile] = None) -> None:
        self.profile = profile or DEFAULT_PROFILE

    def score(self, title: str, year: Optional[int], candidate: MatchCandidate) -> ScoredCandidate:
        local = normalize_title(title, year)
        best_score, best_title = 0.0, candidate.title
        for candidate_title in candidate.all_titles():
            similarity = title_similarity(local, normalize_title(candidate_title, candidate.year))
            if similarity > best_score:
                best_score, best_title = similarity, candidate_title
        adjustment = self.profile.year_adjustment(year, candidate.year)
        final = max(0.0, min(100.0, best_score + adjustment))
        return ScoredCandidate(candidate, round(best_score, 2), adjustment, round(final, 2), best_title)

    @staticmethod
    def _order_key(scored: ScoredCandidate) -> tuple:
        popularity = scored.candidate.popularity if scored.candidate.popularity is not None else -1.0
        return (-scored.score, -scored.title_score, -scored.year_adjustment, -popularity,
                str(scored.candidate.remote_id))

    def rank(self, title: str, year: Optional[int], candidates: Iterable[MatchCandidate]) -> list[ScoredCandidate]:
        best: dict[str, ScoredCandidate] = {}
        for candidate in candidates:
            scored = self.score(title, year, candidate)
            key = str(candidate.remote_id)
            # The same item listed twice: keep its best-scoring listing, whatever the input order.
            if key not in best or self._order_key(scored) < self._order_key(best[key]):
                best[key] = scored
        return sorted(best.values(), key=self._order_key)

    def decide(self, title: str, year: Optional[int], candidates: Iterable[MatchCandidate],
               threshold: float) -> MatchDecision:
        return self.decide_ranked(self.rank(title, year, candidates), threshold)

    def decide_ranked(self, ranked: list[ScoredCandidate], threshold: float) -> MatchDecision:
        """The threshold/ambiguity decision over candidates already scored by :meth:`rank`.

        For plugins that must drop some ranked candidates first (e.g. after confirming each
        one's media type): the kept candidates keep their shared scores and order.
        """
        ranked = sorted(ranked, key=self._order_key)
        if not ranked:
            return MatchDecision(Decision.UNMATCHED, None, ranked, threshold, "no candidates")
        best = ranked[0]
        if best.score < threshold:
            return MatchDecision(Decision.UNMATCHED, None, ranked, threshold,
                                 f"best score {best.score:g} is below the threshold {threshold:g}")
        if len(ranked) > 1 and best.score - ranked[1].score < self.profile.ambiguity_margin:
            return MatchDecision(Decision.AMBIGUOUS, None, ranked, threshold,
                                 f"top candidates score {best.score:g} and {ranked[1].score:g}")
        return MatchDecision(Decision.MATCHED, best, ranked, threshold)


def match_candidates(title: str, year: Optional[int], candidates: Iterable[MatchCandidate], threshold: float,
                     profile: Optional[ScoringProfile] = None) -> MatchDecision:
    return TitleMatcher(profile).decide(title, year, candidates, threshold)
