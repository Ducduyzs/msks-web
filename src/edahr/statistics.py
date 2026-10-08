"""Paper-clustered paired comparisons for the paper's result tables.

Questions of one paper are not independent, so the paper is the sampling
unit throughout:

* ``cluster_sign_flip_test`` — paired randomization test: under H0 the
  per-paper mean difference is symmetric around ``null``, so its sign is
  flipped independently per paper (exact enumeration up to 2^16 papers,
  Monte Carlo beyond). Two-sided, or one-sided ``greater``.
* ``cluster_bootstrap_ci`` — percentile CI of the question-weighted mean
  difference, resampling papers.
* ``paired_effect_size`` — Cohen's d_z on per-paper mean differences.
* ``holm`` — Holm-Bonferroni adjusted p-values for a family of comparisons.
* ``non_inferiority`` — H0: mean diff <= -margin vs H1: > -margin; decided
  by the one-sided sign-flip test and reported with the matching one-sided
  bootstrap lower bound. The margin must be fixed before seeing test data.
"""

from __future__ import annotations

import itertools
import math
import random
from typing import Sequence


def _by_cluster(diffs: Sequence[float], clusters: Sequence[str]) -> dict[str, list[float]]:
    groups: dict[str, list[float]] = {}
    for diff, cluster in zip(diffs, clusters):
        groups.setdefault(str(cluster), []).append(float(diff))
    return groups


def cluster_sign_flip_test(
    diffs: Sequence[float],
    clusters: Sequence[str],
    null: float = 0.0,
    alternative: str = "two-sided",
    iterations: int = 20000,
    seed: int = 42,
) -> float:
    """p-value of the paired cluster sign-flip randomization test."""
    groups = _by_cluster([d - null for d in diffs], clusters)
    sums = [sum(values) for values in groups.values()]
    total = sum(len(values) for values in groups.values())
    if not total:
        return 1.0
    observed = sum(sums) / total

    def extreme(statistic: float) -> bool:
        if alternative == "greater":
            return statistic >= observed - 1e-12
        return abs(statistic) >= abs(observed) - 1e-12

    if len(sums) <= 16:
        hits = count = 0
        for signs in itertools.product((1, -1), repeat=len(sums)):
            count += 1
            hits += extreme(sum(s * v for s, v in zip(signs, sums)) / total)
        return hits / count
    rng = random.Random(seed)
    hits = 1  # include the observed assignment (valid Monte Carlo p-value)
    for _ in range(iterations):
        statistic = sum(v if rng.random() < 0.5 else -v for v in sums) / total
        hits += extreme(statistic)
    return hits / (iterations + 1)


def cluster_bootstrap_ci(
    diffs: Sequence[float],
    clusters: Sequence[str],
    level: float = 0.95,
    iterations: int = 5000,
    seed: int = 42,
) -> tuple[float, float]:
    groups = list(_by_cluster(diffs, clusters).values())
    if not groups:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        sample = [groups[rng.randrange(len(groups))] for _ in groups]
        size = sum(len(group) for group in sample)
        means.append(sum(sum(group) for group in sample) / size)
    means.sort()
    tail = (1.0 - level) / 2.0
    low = means[int(math.floor(tail * (len(means) - 1)))]
    high = means[int(math.ceil((1.0 - tail) * (len(means) - 1)))]
    return low, high


def paired_effect_size(diffs: Sequence[float], clusters: Sequence[str]) -> float:
    """Cohen's d_z over per-paper mean differences (0 when undefined)."""
    means = [sum(v) / len(v) for v in _by_cluster(diffs, clusters).values()]
    if len(means) < 2:
        return 0.0
    mu = sum(means) / len(means)
    sd = math.sqrt(sum((m - mu) ** 2 for m in means) / (len(means) - 1))
    return mu / sd if sd > 0 else 0.0


def holm(p_values: Sequence[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values, in the input order."""
    order = sorted(range(len(p_values)), key=lambda i: p_values[i])
    adjusted = [0.0] * len(p_values)
    running = 0.0
    m = len(p_values)
    for rank, index in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p_values[index]))
        adjusted[index] = running
    return adjusted


def non_inferiority(
    diffs: Sequence[float],
    clusters: Sequence[str],
    margin: float,
    alpha: float = 0.05,
    seed: int = 42,
) -> dict:
    p = cluster_sign_flip_test(diffs, clusters, null=-margin, alternative="greater", seed=seed)
    lower, _ = cluster_bootstrap_ci(diffs, clusters, level=1 - 2 * alpha, seed=seed)
    return {"margin": margin, "p": p, "one_sided_lower": lower,
            "non_inferior": bool(p < alpha)}


def compare(
    first: dict[str, float | None],
    second: dict[str, float | None],
    clusters: dict[str, str],
    margin: float | None = None,
    seed: int = 42,
) -> dict:
    """Paired comparison of two systems' per-question scores (question_id -> value)."""
    keys = [k for k in first if k in second and first[k] is not None and second[k] is not None]
    diffs = [float(first[k]) - float(second[k]) for k in keys]
    groups = [clusters[k] for k in keys]
    low, high = cluster_bootstrap_ci(diffs, groups, seed=seed)
    result = {
        "questions": len(keys),
        "papers": len(set(groups)),
        "mean_diff": sum(diffs) / len(diffs) if diffs else 0.0,
        "ci95": [low, high],
        "p": cluster_sign_flip_test(diffs, groups, seed=seed),
        "d_z": paired_effect_size(diffs, groups),
    }
    if margin is not None:
        result["non_inferiority"] = non_inferiority(diffs, groups, margin, seed=seed)
    return result
