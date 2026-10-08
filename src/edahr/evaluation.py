"""Benchmark metrics for EDAHR.

Retrieval: recall/precision@k, MRR, nDCG, evidence-span recall.
Grounding: citation precision/recall/F1 and page-level provenance accuracy.
Answers: exact match and token F1.
Selective prediction: risk-coverage curve, AURC/E-AURC, selective accuracy.
Systems: latency statistics, adaptive utility, bootstrap CIs and paired
significance testing. Stdlib-only so benchmarks run anywhere.
"""

from __future__ import annotations

import math
import random
import re
import string
from collections import Counter
from collections.abc import Iterable, Sequence


# ---------------------------------------------------------------------------
# Retrieval metrics
# ---------------------------------------------------------------------------

def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    return len(set(retrieved[:k]) & relevant) / max(1, len(relevant))


def precision_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    window = retrieved[:k]
    return len(set(window) & relevant) / max(1, min(k, len(window)))


def hit_rate_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    return 1.0 if set(retrieved[:k]) & relevant else 0.0


def reciprocal_rank(retrieved: list[str], relevant: set[str]) -> float:
    for rank, node_id in enumerate(retrieved, start=1):
        if node_id in relevant:
            return 1.0 / rank
    return 0.0


def mean_reciprocal_rank(
    runs: Iterable[Sequence[str]], golds: Iterable[set[str]]
) -> float:
    values = [reciprocal_rank(list(r), g) for r, g in zip(runs, golds)]
    return sum(values) / max(1, len(values))


def ndcg_at_k(
    retrieved: Sequence[str], graded_relevance: dict[str, float], k: int
) -> float:
    """nDCG@k with exponential gains log2(rel + 1)."""

    def dcg(order: Sequence[str]) -> float:
        return sum(
            (2.0 ** graded_relevance.get(node_id, 0.0) - 1.0) / math.log2(rank + 1)
            for rank, node_id in enumerate(order[:k], start=1)
        )

    ideal = sorted(graded_relevance.values(), reverse=True)
    idcg = sum(
        (2.0 ** rel - 1.0) / math.log2(rank + 1)
        for rank, rel in enumerate(ideal[:k], start=1)
    )
    return dcg(retrieved) / idcg if idcg > 0 else 0.0


def _token_f1(first: str, second: str) -> float:
    first_tokens = first.split()
    second_tokens = second.split()
    if not first_tokens or not second_tokens:
        return 0.0
    first_counts = Counter(first_tokens)
    second_counts = Counter(second_tokens)
    common = sum(
        min(count, second_counts[token])
        for token, count in first_counts.items()
        if token in second_counts
    )
    if not common:
        return 0.0
    precision = common / len(first_tokens)
    recall = common / len(second_tokens)
    return 2 * precision * recall / (precision + recall)


def evidence_span_recall(
    predicted_texts: Sequence[str],
    gold_quotes: Sequence[str],
    tau: float = 0.5,
) -> float:
    """Share of gold evidence spans covered by predicted evidence.

    A gold quote counts as covered when some predicted evidence reaches token
    F1 >= tau with it or contains it -- robust to segmentation differences.
    """
    if not gold_quotes:
        return 0.0
    gold = [" ".join(q.split()).casefold() for q in gold_quotes]
    predicted = [" ".join(p.split()).casefold() for p in predicted_texts]
    covered = sum(
        1
        for quote in gold
        if any(_token_f1(quote, pred) >= tau or (quote in pred) for pred in predicted)
    )
    return covered / len(gold)


# ---------------------------------------------------------------------------
# Grounding metrics
# ---------------------------------------------------------------------------

def citation_precision(predicted: Iterable[str], supported: set[str]) -> float:
    predicted_set = set(predicted)
    return len(predicted_set & supported) / max(1, len(predicted_set))


def citation_recall(predicted: Iterable[str], supported: set[str]) -> float:
    supported_set = set(supported)
    return len(set(predicted) & supported_set) / max(1, len(supported_set))


def citation_f1(predicted: Iterable[str], supported: set[str]) -> float:
    precision = citation_precision(predicted, supported)
    recall = citation_recall(predicted, supported)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def provenance_accuracy(
    predicted_provenance: Sequence[tuple[str, int, int]],
    gold_provenance: set[tuple[str, int]],
) -> float:
    """Fraction of citations whose (source, page range) hits a gold page."""
    if not predicted_provenance or not gold_provenance:
        return 0.0
    correct = 0
    for source, page_start, page_end in predicted_provenance:
        if any(
            g_source == source and page_start <= g_page <= page_end
            for g_source, g_page in gold_provenance
        ):
            correct += 1
    return correct / len(predicted_provenance)


def evidence_density(relevant_tokens: int, context_tokens: int) -> float:
    return relevant_tokens / max(1, context_tokens)


# ---------------------------------------------------------------------------
# Answer metrics
# ---------------------------------------------------------------------------

def normalize_answer(answer: str) -> str:
    return " ".join(answer.lower().split())


def normalize_qasper_answer(answer: str) -> str:
    """QASPER's official SQuAD-v1.1 answer normalization."""
    answer = answer.lower()
    answer = "".join(char for char in answer if char not in string.punctuation)
    answer = re.sub(r"\b(a|an|the)\b", " ", answer)
    return " ".join(answer.split())


def answer_exact_match(prediction: str, gold: str) -> float:
    return float(normalize_answer(prediction) == normalize_answer(gold))


def answer_token_f1(prediction: str, gold: str) -> float:
    return _token_f1(normalize_answer(prediction), normalize_answer(gold))


def qasper_answer_exact_match(prediction: str, references: Sequence[str]) -> float:
    return max(
        (float(normalize_qasper_answer(prediction) == normalize_qasper_answer(reference))
         for reference in references),
        default=0.0,
    )


def qasper_answer_token_f1(prediction: str, references: Sequence[str]) -> float:
    normalized_prediction = normalize_qasper_answer(prediction)
    return max(
        (_token_f1(normalized_prediction, normalize_qasper_answer(reference))
         for reference in references),
        default=0.0,
    )


def qasper_evidence_f1(predicted: Sequence[str], reference_sets: Sequence[Sequence[str]]) -> float:
    """Official max-over-annotators paragraph Evidence F1.

    Mirrors ``paragraph_f1_score`` of the official evaluator: overlap is
    counted on sets but precision/recall divide by the *list* lengths.
    """
    predicted = list(predicted)

    def score(reference: Sequence[str]) -> float:
        reference = list(reference)
        if not predicted and not reference:
            return 1.0
        overlap = len(set(predicted) & set(reference))
        if not overlap:
            return 0.0
        precision = overlap / len(predicted)
        recall = overlap / len(reference)
        return 2.0 * precision * recall / (precision + recall)

    return max((score(reference) for reference in reference_sets), default=0.0)


# ---------------------------------------------------------------------------
# Selective prediction / risk-coverage
# ---------------------------------------------------------------------------

def _confidence_order(
    accuracies: Sequence[float], confidences: Sequence[float]
) -> list[int]:
    return sorted(range(len(accuracies)), key=lambda i: confidences[i], reverse=True)


def risk_coverage_curve(
    accuracies: Sequence[float], confidences: Sequence[float]
) -> tuple[list[float], list[float]]:
    """Sort by confidence desc; return (coverage, cumulative risk) points."""
    if len(accuracies) != len(confidences) or not accuracies:
        return [], []
    coverages: list[float] = []
    risks: list[float] = []
    correct_sum = 0.0
    for count, index in enumerate(_confidence_order(accuracies, confidences), start=1):
        correct_sum += accuracies[index]
        coverages.append(count / len(accuracies))
        risks.append(1.0 - correct_sum / count)
    return coverages, risks


def aurc(accuracies: Sequence[float], confidences: Sequence[float]) -> float:
    """Area under the risk-coverage curve."""
    _, risks = risk_coverage_curve(accuracies, confidences)
    return sum(risks) / max(1, len(risks))


def e_aurc(accuracies: Sequence[float], confidences: Sequence[float]) -> float:
    """Excess AURC over the oracle confidence ordering."""
    model_area = aurc(accuracies, confidences)
    ordered = sorted(accuracies, reverse=True)
    oracle_risk_sum = 0.0
    correct_sum = 0.0
    for count, value in enumerate(ordered, start=1):
        correct_sum += value
        oracle_risk_sum += 1.0 - correct_sum / count
    oracle_area = oracle_risk_sum / max(1, len(ordered))
    return model_area - oracle_area


def selective_accuracy_at_coverage(
    accuracies: Sequence[float],
    confidences: Sequence[float],
    coverage_target: float,
) -> float:
    if not accuracies:
        return 0.0
    order = _confidence_order(accuracies, confidences)
    keep = max(1, round(min(1.0, max(0.01, coverage_target)) * len(order)))
    selected = [accuracies[i] for i in order[:keep]]
    return sum(selected) / keep


# ---------------------------------------------------------------------------
# System-level metrics
# ---------------------------------------------------------------------------

def latency_stats(latencies_ms: Sequence[float]) -> dict[str, float]:
    if not latencies_ms:
        return {"mean_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    values = sorted(latencies_ms)

    def percentile(fraction: float) -> float:
        rank = max(0, min(len(values) - 1, math.ceil(fraction * len(values)) - 1))
        return values[rank]

    return {
        "mean_ms": sum(values) / len(values),
        "p50_ms": float(values[len(values) // 2]),
        "p95_ms": percentile(0.95),
        "max_ms": values[-1],
    }


def adaptive_utility(answer_score: float, context_tokens: int, latency_ms: float) -> float:
    return answer_score - 0.00002 * context_tokens - 0.00001 * latency_ms


# ---------------------------------------------------------------------------
# Statistics: bootstrap CI and paired significance
# ---------------------------------------------------------------------------

def bootstrap_ci(
    samples: Sequence[float],
    iterations: int = 1000,
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float, float]:
    """Percentile bootstrap confidence interval for the sample mean."""
    if not samples:
        return 0.0, 0.0
    rng = random.Random(seed)
    size = len(samples)
    means: list[float] = []
    for _ in range(iterations):
        draw = [samples[rng.randrange(size)] for _ in range(size)]
        means.append(sum(draw) / size)
    means.sort()
    low = max(0, min(len(means) - 1, int(math.floor((alpha / 2) * len(means)))))
    high = max(low, min(len(means) - 1, int(math.ceil((1 - alpha / 2) * len(means))) - 1))
    return means[low], means[high]


def paired_cluster_bootstrap(
    diffs: Sequence[float],
    clusters: Sequence[str],
    iterations: int = 1000,
    seed: int = 42,
) -> tuple[float, float]:
    """95% CI for mean(diff) under cluster (paper-level) resampling."""
    by_cluster: dict[str, list[float]] = {}
    for diff, cluster in zip(diffs, clusters):
        by_cluster.setdefault(str(cluster), []).append(float(diff))
    groups = list(by_cluster.values())
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(iterations):
        sample = [value for _ in range(len(groups)) for value in groups[rng.randrange(len(groups))]]
        if sample:
            means.append(sum(sample) / len(sample))
    if not means:
        return 0.0, 0.0
    means.sort()
    low = means[int(0.025 * len(means))]
    high = means[min(len(means) - 1, int(0.975 * len(means)))]
    return low, high


def paired_bootstrap_test(
    first: Sequence[float],
    second: Sequence[float],
    iterations: int = 1000,
    seed: int = 42,
) -> float:
    """Two-sided paired bootstrap p-value that mean(first) != mean(second)."""
    if len(first) != len(second) or not first:
        return 1.0
    observed = sum(first) / len(first) - sum(second) / len(second)
    rng = random.Random(seed)
    size = len(first)
    deltas = [first[i] - second[i] for i in range(size)]
    extreme = 0
    for _ in range(iterations):
        resampled = [deltas[rng.randrange(size)] * (1 if rng.random() < 0.5 else -1) for _ in range(size)]
        mean_shift = sum(resampled) / size
        if abs(mean_shift) >= abs(observed) - 1e-12:
            extreme += 1
    return (extreme + 1.0) / (iterations + 1.0)


def aggregate(records: Iterable[dict[str, float]]) -> dict[str, float]:
    """Macro-average numeric fields across per-query records."""
    collected: dict[str, list[float]] = {}
    for record in records:
        for key, value in record.items():
            if isinstance(value, (int, float)):
                collected.setdefault(key, []).append(float(value))
    return {
        key: sum(values) / len(values)
        for key, values in sorted(collected.items())
        if values
    }
