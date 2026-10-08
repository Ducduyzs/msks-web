"""Agreement Ranking: cross-encoder rank fused with SBERT query-leaf similarity.

Derived in analysis/v10_selector_design.md: RAPTOR's advantage on QASPER came
from its SBERT query-leaf signal (``multi-qa-mpnet-base-cos-v1``), which drops
leaves the cross-encoder over-ranks. Agreement Ranking keeps that signal and
drops the tree, summaries and clustering.

The order is reciprocal-rank fusion of the two rankings. The packer downstream
expects cross-encoder-scale utilities, so the fused order receives the
cross-encoder's own score multiset (i-th fused leaf gets the i-th highest
cross-encoder score): only the order changes, never the score distribution.
"""

from __future__ import annotations

from typing import Mapping, Sequence

SBERT_MODEL = "sentence-transformers/multi-qa-mpnet-base-cos-v1"
RRF_K = 60


def agreement_ranking(
    rerank: Sequence[tuple[str, float]],
    similarity: Mapping[str, float],
    k: int = RRF_K,
) -> list[tuple[str, float]]:
    """Fuse a cross-encoder ranking with query-leaf similarities.

    ``rerank`` is (leaf_id, cross-encoder score) sorted best first; every
    leaf must have a ``similarity``. Ties break by leaf id for determinism.
    """
    ids = [leaf for leaf, _ in rerank]
    missing = [leaf for leaf in ids if leaf not in similarity]
    if missing:
        raise KeyError(f"no similarity for {len(missing)} leaves")
    by_similarity = sorted(ids, key=lambda leaf: (-similarity[leaf], leaf))
    fused = {leaf: 0.0 for leaf in ids}
    for order in (ids, by_similarity):
        for rank, leaf in enumerate(order, 1):
            fused[leaf] += 1.0 / (k + rank)
    order = sorted(ids, key=lambda leaf: (-fused[leaf], leaf))
    scores = sorted((score for _, score in rerank), reverse=True)
    return list(zip(order, scores))
