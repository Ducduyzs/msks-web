from __future__ import annotations

from typing import Mapping

from .config import Settings
from .interfaces import Reranker
from .policy import AdaptiveMergePolicy, decide_candidates, group_members_by_ancestor
from .schemas import LEVEL_ORDER, Hierarchy, Level, MergeDecision, QueryType, level_rank


MAX_LEVEL_BY_QUERY_TYPE: dict[QueryType, Level] = {
    QueryType.FACTOID: Level.PARENT,
    QueryType.EXPLANATORY: Level.SECTION,
    QueryType.COMPARATIVE: Level.DOCUMENT,
    QueryType.GLOBAL: Level.DOCUMENT,
}


def max_level_for_query(query_type: QueryType) -> Level:
    """Guardrail: how far up the hierarchy this query type may expand."""
    return MAX_LEVEL_BY_QUERY_TYPE[query_type]


def _selected_tokens(hierarchy: Hierarchy, selected: Mapping[str, float]) -> int:
    return sum(hierarchy.node(node_id).token_count for node_id in selected)


def expand_selection(
    query: str,
    query_type: QueryType,
    hierarchy: Hierarchy,
    reranker: Reranker,
    policy: AdaptiveMergePolicy,
    settings: Settings,
    selected_scores: dict[str, float],
    score_cache: dict[str, float] | None = None,
    start_rank: int | None = None,
) -> tuple[dict[str, float], list[MergeDecision], list[str]]:
    """Iteratively expand the evidence selection up the hierarchy.

    Starting from the current selection (usually children plus any accepted
    parents), repeatedly compare ``U(ancestor)`` against
    ``U(retained descendants)`` one level at a time::

        child -> parent -> section -> document

    Guardrails implemented here:
      * query-type ceiling (factoid never leaves the parent level);
      * ``expansion_max_depth`` iterations;
      * early stopping when a level yields no accepted merge or the total
        accepted utility delta falls under ``expansion_epsilon``;
      * token-budget headroom so expansion cannot silently blow the budget.

    Returns the updated selection (node -> score), all expansion decisions and
    a human-readable drill-down trace.
    """
    cache = score_cache if score_cache is not None else {}
    selected = dict(selected_scores)
    decisions: list[MergeDecision] = []
    trace: list[str] = []
    ceiling = min(level_rank(max_level_for_query(query_type)), level_rank(Level.DOCUMENT))
    if start_rank is None:
        start_rank = min(
            (level_rank(hierarchy.node(node_id).level) for node_id in selected),
            default=0,
        )
    current_rank = max(0, min(start_rank, ceiling - 1)) if ceiling > 0 else 0
    depth = 0

    while depth < settings.expansion_max_depth and current_rank < ceiling:
        next_level = LEVEL_ORDER[current_rank + 1]
        member_level = LEVEL_ORDER[current_rank]
        projected_tokens = _selected_tokens(hierarchy, selected)
        headroom = settings.context_token_budget * settings.expansion_headroom
        if projected_tokens > headroom:
            trace.append(
                f"stop@{next_level.value}: token headroom reached "
                f"({projected_tokens:.0f} > {headroom:.0f})"
            )
            break
        grouped = group_members_by_ancestor(hierarchy, selected, next_level)
        candidates = sorted(
            ancestor_id
            for ancestor_id, members in grouped.items()
            if len(members) >= settings.min_child_hits
        )
        if not candidates:
            trace.append(f"stop@{next_level.value}: no candidate with enough retrieved members")
            break
        level_decisions = decide_candidates(
            query=query,
            query_type=query_type,
            hierarchy=hierarchy,
            reranker=reranker,
            policy=policy,
            settings=settings,
            candidates=candidates,
            member_scores=selected,
            candidate_level=next_level,
            member_level=member_level,
            candidate_score_cache=cache,
        )
        decisions.extend(level_decisions)
        accepted = [decision for decision in level_decisions if decision.accepted]
        if not accepted:
            trace.append(
                f"stop@{next_level.value}: utility comparison favoured retained "
                f"{len(candidates)} candidate(s)' members"
            )
            break
        gained = 0.0
        consumed: set[str] = set()
        for decision in accepted:
            gained += max(0.0, decision.utility)
            consumed.update(decision.child_ids)
            selected.pop(decision.parent_id, None)
            for member_id in decision.child_ids:
                consumed.add(member_id)
        for member_id in list(selected):
            if member_id in consumed:
                del selected[member_id]
        for decision in accepted:
            score = cache.get(decision.parent_id, decision.parent_utility)
            selected[decision.parent_id] = float(score)
        total_gain = gained
        trace.append(
            f"{next_level.value}: merged {len(accepted)}/{len(candidates)} "
            f"candidates (utility delta {gained:+.3f})"
        )
        if gained < settings.expansion_epsilon:
            trace.append(f"stop@{next_level.value}: marginal utility below epsilon")
            break
        current_rank += 1
        depth += 1

    return selected, decisions, trace
