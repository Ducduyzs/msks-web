from __future__ import annotations

from typing import Mapping

from .config import Settings
from .schemas import ContextBlock, Hierarchy, QueryType
from .text import containment, jaccard, token_estimate, token_set, truncate_to_fit


def _deduplicate(
    candidates: list[dict], hierarchy: Hierarchy, threshold: float
) -> list[dict]:
    """Greedy near-duplicate removal by lexical Jaccard / containment."""
    kept: list[dict] = []
    kept_token_sets: list[set[str]] = []
    for candidate in candidates:
        candidate_tokens = token_set(candidate["text"])
        duplicate = False
        for existing in kept_token_sets:
            if jaccard(candidate_tokens, existing) >= threshold or (
                containment(candidate_tokens, existing) >= min(0.95, threshold + 0.05)
                and len(candidate_tokens) < len(existing)
            ):
                duplicate = True
                break
        if not duplicate:
            kept.append(candidate)
            kept_token_sets.append(candidate_tokens)
    return kept


def _knapsack(items: list[tuple[int, float, int]], capacity: int) -> list[int]:
    """Maximize utility within `capacity` bucket weights; prefer lighter packs on ties."""
    best_values = [-1.0] * (capacity + 1)
    best_choices: list[tuple[int, ...]] = [()] * (capacity + 1)
    best_values[0] = 0.0
    for item_index, utility, weight in items:
        if weight <= 0 or weight > capacity:
            continue
        for cap in range(capacity, weight - 1, -1):
            previous = best_values[cap - weight]
            if previous < 0:
                continue
            candidate_value = round(previous + utility, 9)
            current = best_values[cap]
            if candidate_value > current or (
                abs(candidate_value - current) < 1e-9
                and len(best_choices[cap - weight]) + 1 < len(best_choices[cap])
            ):
                best_values[cap] = candidate_value
                best_choices[cap] = (*best_choices[cap - weight], item_index)
    full = max(range(capacity + 1), key=lambda c: (best_values[c], -c))
    return list(best_choices[full])


def _source_counts(selected: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in selected:
        counts[item["source"]] = counts.get(item["source"], 0) + 1
    return counts


def _enforce_diversity(
    selected: list[dict],
    pool: list[dict],
    budget_tokens: int,
    settings: Settings,
    query_type: QueryType,
) -> list[dict]:
    """Cap single-source share; guarantee minimum distinct sources for comparative queries.

    Repair strategy: prefer *swapping* an overflowing-source block with the
    best unused block from another source that still fits the strict budget;
    only drop outright while another source remains represented; accept the
    violation when the corpus offers no alternative source.
    """

    def tokens_of(items: list[dict]) -> int:
        return sum(item["token_count"] for item in items)

    def best_replacement(victim: dict, running_used: int) -> dict | None:
        present_ids = {item["node_id"] for item in selected}
        options = [
            item for item in pool
            if item["source"] != victim["source"]
            and item["node_id"] not in present_ids
            and running_used - victim["token_count"] + item["token_count"] <= budget_tokens
        ]
        return max(options, key=lambda item: item["utility"], default=None)

    for _ in range(4 * settings.final_context_k):
        counts = _source_counts(selected)
        total = max(1, len(selected))
        overflowing = [
            source
            for source, count in counts.items()
            if count / total > settings.max_source_share + 1e-9
        ]
        if not overflowing:
            break
        victim_source = max(overflowing, key=lambda s: counts[s])
        victim = min(
            (item for item in selected if item["source"] == victim_source),
            key=lambda item: item["utility"],
        )
        used = tokens_of(selected)
        replacement = best_replacement(victim, used)
        if replacement is not None:
            selected.remove(victim)
            pool = [item for item in pool if item["node_id"] != replacement["node_id"]]
            selected.append(replacement)
            continue
        remaining_sources = set(counts) - {victim_source}
        if remaining_sources:
            selected.remove(victim)
            continue
        break  # single-source corpus: violation unavoidable

    distinct_available = len({item["source"] for item in pool} | {item["source"] for item in selected})
    min_needed = min(settings.comparative_min_sources, distinct_available)
    for _ in range(settings.comparative_min_sources):
        counts = _source_counts(selected)
        if query_type != QueryType.COMPARATIVE or len(counts) >= min_needed:
            break
        present = set(counts)
        additions = sorted(
            (
                item for item in pool
                if item["source"] not in present
                and tokens_of(selected) + item["token_count"] <= budget_tokens
            ),
            key=lambda item: item["utility"],
            reverse=True,
        )
        if not additions:
            break
        selected.append(additions[0])
        pool = [item for item in pool if item["node_id"] != additions[0]["node_id"]]
    return selected


def assemble_context(
    hierarchy: Hierarchy,
    selected_scores: Mapping[str, float],
    query_type: QueryType,
    settings: Settings,
) -> list[ContextBlock]:
    """Budgeted evidence selection with guardrails.

    Pipeline: near-duplicate removal -> top-k candidates -> exact 0/1 knapsack
    over token buckets (strict budget, unlike a skip-if-overflow greedy) ->
    source-diversity repair -> sentence-boundary truncation as last resort.
    """
    budget = max(1, settings.context_token_budget)
    candidates = [
        {
            "node_id": node_id,
            "utility": float(score),
            "token_count": hierarchy.node(node_id).token_count,
            "text": hierarchy.node(node_id).text,
            "source": hierarchy.node(node_id).source,
        }
        for node_id, score in selected_scores.items()
        if node_id in hierarchy.nodes
    ]
    candidates.sort(key=lambda item: item["utility"], reverse=True)
    candidates = _deduplicate(candidates, hierarchy, settings.context_dedup_threshold)[: settings.final_context_k]
    if not candidates:
        return []

    bucket = max(1, settings.knapsack_token_bucket)
    weighted_items = [
        (index, item["utility"], -(-item["token_count"] // bucket))
        for index, item in enumerate(candidates)
    ]
    chosen_indexes = _knapsack(weighted_items, capacity=max(1, budget // bucket))
    selected = [candidates[index] for index in chosen_indexes]

    pool = [item for item in candidates if item not in selected]
    selected = _enforce_diversity(selected, pool, budget, settings, query_type)

    used = sum(item["token_count"] for item in selected)
    if not selected and candidates:
        fallback = dict(candidates[0])
        fallback["text"] = truncate_to_fit(fallback["text"], budget)
        fallback["token_count"] = token_estimate(fallback["text"])
        fallback["truncated"] = True
        selected.append(fallback)
    elif used > budget:
        selected.sort(key=lambda item: item["utility"], reverse=True)
        trimmed: list[dict] = []
        running = 0
        for item in selected:
            if running >= budget:
                break
            room = budget - running
            if item["token_count"] <= room:
                trimmed.append({**item, "truncated": False})
                running += item["token_count"]
            else:
                clipped = dict(item)
                clipped["text"] = truncate_to_fit(item["text"], room)
                clipped["token_count"] = token_estimate(clipped["text"])
                clipped["truncated"] = True
                if clipped["token_count"] <= room:
                    trimmed.append(clipped)
                    running += clipped["token_count"]
        selected = trimmed

    selected.sort(key=lambda item: item["utility"], reverse=True)
    blocks: list[ContextBlock] = []
    for position, item in enumerate(selected, start=1):
        node = hierarchy.node(item["node_id"])
        blocks.append(
            ContextBlock(
                context_id=f"C{position}",
                node_id=node.node_id,
                level=node.level,
                text=item.get("text", node.text),
                source=node.source,
                page_start=node.page_start,
                page_end=node.page_end,
                evidence_ids=node.evidence_child_ids or (node.node_id,),
                utility=round(float(item["utility"]), 6),
                token_count=item["token_count"],
                char_start=node.char_start,
                char_end=node.char_end,
                truncated=bool(item.get("truncated", False)),
            )
        )
    return blocks
