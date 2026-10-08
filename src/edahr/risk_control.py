"""Conformal risk control for the expanded-leaf citation threshold (H5).

Expansion adds leaves that retrieval never ranked. Citing such a leaf when it
is not gold evidence is *harmful attribution drift*. A threshold ``lam`` on
the verifier support of non-retrieved leaves trades rescue against drift;
Conformal Risk Control (Angelopoulos et al., ICLR 2024) picks the smallest
``lam`` whose expected drift loss on a new exchangeable unit is <= ``alpha``:

    lam_hat = min{ lam : n/(n+1) * R_n(lam) + B/(n+1) <= alpha }

valid when each unit's loss is bounded by ``B`` and non-increasing in ``lam``.
Units are *papers* (questions of one paper are not exchangeable), so the
guarantee is per new paper. ``float("inf")`` (cite retrieved leaves only)
has zero drift by construction and is the fallback when no finite ``lam``
qualifies.
"""

from __future__ import annotations

import math
from typing import Callable, Mapping, Sequence

INF = float("inf")


def crc_threshold(
    unit_losses: Sequence[Callable[[float], float]],
    grid: Sequence[float],
    alpha: float,
    bound: float = 1.0,
) -> float:
    """Smallest grid threshold satisfying the CRC inequality (INF if none)."""
    n = len(unit_losses)
    if n == 0:
        return INF
    for lam in sorted(grid):
        risk = sum(loss(lam) for loss in unit_losses) / n
        if n / (n + 1) * risk + bound / (n + 1) <= alpha:
            return lam
    return INF


def check_monotone(loss: Callable[[float], float], grid: Sequence[float]) -> bool:
    values = [loss(lam) for lam in sorted(grid)] + [loss(INF)]
    return all(later <= earlier + 1e-12 for earlier, later in zip(values, values[1:]))


def paper_folds(paper_ids: Sequence[str], folds: int) -> list[set[str]]:
    """Deterministic paper-level folds (sorted ids, round-robin)."""
    ordered = sorted(set(paper_ids))
    buckets: list[set[str]] = [set() for _ in range(max(1, min(folds, len(ordered))))]
    for index, paper_id in enumerate(ordered):
        buckets[index % len(buckets)].add(paper_id)
    return buckets


def mean(values: Sequence[float]) -> float:
    values = [value for value in values if value is not None and not math.isnan(value)]
    return sum(values) / len(values) if values else float("nan")


def group_mean(values_by_unit: Mapping[str, Sequence[float]]) -> dict[str, float]:
    return {unit: mean(values) for unit, values in values_by_unit.items()}
