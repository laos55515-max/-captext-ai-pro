"""Dynamic-programming phrase segmentation, genre aware."""
from __future__ import annotations

from dataclasses import dataclass
from math import inf

from app.core.models import AudioProfile, Phrase, Word

STRONG_PUNCT = ".!?…"
WEAK_PUNCT = ",;:—–-"
DANGLING = {
    "и", "а", "но", "в", "во", "на", "с", "со", "к", "по", "за", "из", "от", "до", "у", "о",
    "об", "при", "для", "не", "что", "как", "же", "бы", "ли", "та", "то", "й", "та", "з",
    "the", "a", "an", "of", "to", "in", "on", "and", "or", "for", "with",
}


@dataclass(frozen=True)
class SegmentParams:
    max_words: int = 6
    target_words: int = 3
    max_chars: int = 30
    max_block_seconds: float = 2.5
    gap_weight: float = 4.0
    punct_strong: float = 3.0
    punct_weak: float = 1.5
    dangling_penalty: float = 2.0
    size_weight: float = 1.0
    duration_weight: float = 0.5


GENRE_PARAMS: dict[str, SegmentParams] = {
    "rap": SegmentParams(max_words=2, target_words=2, max_chars=18, max_block_seconds=1.4,
                         gap_weight=2.0, size_weight=2.0),
    "deep_house_phonk": SegmentParams(max_words=3, target_words=2, max_chars=20,
                                      max_block_seconds=1.8, gap_weight=2.5, size_weight=1.6),
    "pop_dance": SegmentParams(max_words=5, target_words=3, max_chars=26,
                               max_block_seconds=2.6, gap_weight=4.5),
    "chanson_acoustic": SegmentParams(max_words=7, target_words=5, max_chars=34,
                                      max_block_seconds=3.4, gap_weight=5.5,
                                      punct_strong=3.5, punct_weak=2.0),
    "speech_podcast": SegmentParams(max_words=6, target_words=4, max_chars=30,
                                    max_block_seconds=3.0, gap_weight=4.0,
                                    punct_strong=3.5, punct_weak=1.8),
}


class PhraseSegmenter:
    """Optimal segmentation by DP over a linguistic + rhythmic cost function."""

    def __init__(self, params: SegmentParams | None = None) -> None:
        self.params = params

    def params_for(self, profile: AudioProfile | None) -> SegmentParams:
        if self.params is not None:
            return self.params
        genre = profile.genre if profile else "speech_podcast"
        return GENRE_PARAMS.get(genre, GENRE_PARAMS["speech_podcast"])

    # --------------------------------------------------------------- costs
    def _cost(self, words: list[Word], i: int, j: int, p: SegmentParams) -> float:
        seg = words[i:j]
        n = len(words)
        chars = sum(len(w.text) + 1 for w in seg) - 1
        if chars > p.max_chars and len(seg) > 1:
            return inf
        cost = p.size_weight * (len(seg) - p.target_words) ** 2

        gap_after = (words[j].start - words[j - 1].end) if j < n else 1.0
        cost -= p.gap_weight * min(max(gap_after, 0.0), 0.6)

        last = seg[-1].text.rstrip()
        if last and last[-1] in STRONG_PUNCT:
            cost -= p.punct_strong
        elif last and last[-1] in WEAK_PUNCT:
            cost -= p.punct_weak

        tail = _bare(seg[-1].text)
        if tail in DANGLING or seg[-1].pos in {"ADP", "CCONJ", "DET", "SCONJ"}:
            cost += p.dangling_penalty

        dur = seg[-1].end - seg[0].start
        cost += p.duration_weight * max(0.0, dur - p.max_block_seconds) * 4.0
        if dur > p.max_block_seconds * 2.0:
            cost += 20.0
        # internal pause inside a block is ugly
        for a, b in zip(seg, seg[1:]):
            g = b.start - a.end
            if g > 0.55:
                cost += 2.5 * min(g, 1.5)
        return cost

    # ------------------------------------------------------------ main api
    def segment_words(self, words: list[Word], profile: AudioProfile | None = None) -> list[Phrase]:
        words = [w for w in words if w.text.strip()]
        if not words:
            return []
        p = self.params_for(profile)
        n = len(words)
        best = [inf] * (n + 1)
        prev = [0] * (n + 1)
        best[0] = 0.0
        for j in range(1, n + 1):
            for i in range(max(0, j - p.max_words), j):
                if best[i] == inf:
                    continue
                c = self._cost(words, i, j, p)
                if c == inf:
                    continue
                if best[i] + c < best[j]:
                    best[j] = best[i] + c
                    prev[j] = i
        # reconstruct
        cuts: list[int] = [n]
        k = n
        guard = 0
        while k > 0 and guard < n + 5:
            k = prev[k]
            cuts.append(k)
            guard += 1
        cuts.reverse()
        phrases: list[Phrase] = []
        for a, b in zip(cuts, cuts[1:]):
            if b > a:
                phrases.append(Phrase(words=[w.model_copy(deep=True) for w in words[a:b]]))
        return phrases


def _bare(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum() or ch in "'’")
