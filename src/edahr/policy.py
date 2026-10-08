from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Mapping, Sequence

from .config import Settings
from .interfaces import Reranker
from .schemas import Hierarchy, Hit, Level, MergeDecision, MergeFeatures, QueryType, level_rank
from .text import jaccard, token_set


class AdaptiveMergePolicy:
    """Utility-based adaptive merge controller.

    A merge is accepted only when the estimated utility of the candidate node
    exceeds the utility of the retained member set plus a margin::

        U(candidate) + evidence_gain_weight * gain >= U(members) + margin
                                                  + cost_penalty * delta_cost

    ``probability`` keeps a calibrated scalar view of U(candidate) (hand-tuned
    logistic or a TorchScript checkpoint) for monitoring and legacy thresholds;
    the decision itself is the explicit utility comparison above.
    """

    def __init__(
        self,
        threshold: float = 0.56,
        margin: float = 0.04,
        evidence_gain_weight: float = 0.35,
        cost_penalty: float = 0.5,
        checkpoint: str | Path | None = None,
    ):
        self.threshold = threshold
        self.margin = margin
        self.evidence_gain_weight = evidence_gain_weight
        self.cost_penalty = cost_penalty
        self.model = None
        self.model_kind = "prior"
        if checkpoint:
            path = Path(checkpoint)
            if path.suffix in {".joblib", ".pkl"}:
                import joblib

                bundle = joblib.load(path)
                self.model = bundle["model"] if isinstance(bundle, dict) else bundle
                if isinstance(bundle, dict) and "threshold" in bundle:
                    self.threshold = float(bundle["threshold"])
                self.model_kind = "sklearn"
            else:
                import torch

                self.model = torch.jit.load(str(path), map_location="cpu").eval()
                self.model_kind = "torchscript"

    def probability(self, features: MergeFeatures) -> float:
        """Calibrated estimate of U(candidate) in [0, 1]."""
        if self.model is not None:
            if self.model_kind == "sklearn":
                import numpy as np

                vector = np.asarray([features.vector()], dtype=np.float32)
                return float(self.model.predict_proba(vector)[0, 1])
            import torch

            with torch.inference_mode():
                value = self.model(torch.tensor([features.vector()], dtype=torch.float32))
                return float(torch.sigmoid(value).flatten()[0])
        logit = (
            1.55 * features.relevance
            + 0.90 * features.coverage
            + 0.45 * features.coherence
            + 1.05 * features.density
            + 0.40 * features.query_global
            + 0.22 * features.query_explanatory
            - 0.85 * features.noise
            - 0.75 * features.cost
            - 0.28 * features.query_factoid
            - 0.95
        )
        return 1.0 / (1.0 + math.exp(-logit))


class StaticMergePolicy(AdaptiveMergePolicy):
    """B4 baseline: always merge when enough members are retrieved."""

    force_accept = True

    def probability(self, features: MergeFeatures) -> float:
        return 1.0


class NeverMergePolicy(AdaptiveMergePolicy):
    """Flat-retrieval baseline: never merge any members upward."""

    force_accept = False

    def probability(self, features: MergeFeatures) -> float:
        return 0.0


def policy_from_checkpoint(
    settings: Settings, checkpoint: str | Path | None, *, enabled: bool = True
) -> AdaptiveMergePolicy:
    """Build one runtime gate and fail early on a missing checkpoint."""
    if not enabled:
        return NeverMergePolicy()
    path = Path(checkpoint).expanduser() if checkpoint else None
    if path is not None and not path.is_file():
        raise FileNotFoundError(f"Policy checkpoint does not exist: {path}")
    return AdaptiveMergePolicy(
        threshold=settings.merge_threshold,
        margin=settings.merge_margin,
        evidence_gain_weight=settings.evidence_gain_weight,
        cost_penalty=settings.cost_penalty,
        checkpoint=path,
    )


def policies_from_settings(settings: Settings) -> tuple[AdaptiveMergePolicy, AdaptiveMergePolicy]:
    """Return independent parent and higher-level gates from runtime settings."""
    parent = policy_from_checkpoint(
        settings, settings.parent_policy_checkpoint, enabled=settings.enable_parent_expansion
    )
    section = policy_from_checkpoint(
        settings, settings.section_policy_checkpoint, enabled=settings.enable_section_expansion
    )
    return parent, section


def _features(
    hierarchy: Hierarchy,
    candidate_id: str,
    member_scores: Mapping[str, float],
    query_type: QueryType,
    token_budget: int,
    candidate_score: float,
    total_members: int,
    query: str = "",
) -> MergeFeatures:
    import math

    candidate = hierarchy.node(candidate_id)
    member_ids = list(member_scores)
    # Relevance: direct query-candidate score after neural reranking (not the
    # mean of child scores, which hides parent-level mismatch).
    relevance = candidate_score
    coverage = len(member_ids) / max(1, total_members)
    # Coherence: internal homogeneity of consecutive members (not child-parent
    # lexical overlap, which is trivially ~1 because parents are built from children).
    pair_scores: list[float] = []
    for prev_id, next_id in zip(member_ids, member_ids[1:]):
        prev_tokens = token_set(hierarchy.node(prev_id).text)
        next_tokens = token_set(hierarchy.node(next_id).text)
        if prev_tokens or next_tokens:
            pair_scores.append(jaccard(prev_tokens, next_tokens))
    coherence = sum(pair_scores) / len(pair_scores) if pair_scores else 0.0
    # Evidence density: share of candidate tokens covered by retrieved relevant
    # members -- the quantity the method name refers to.
    member_tokens = sum(hierarchy.node(m).token_count for m in member_ids)
    total_tokens = max(1, candidate.token_count)
    density_value = min(1.0, member_tokens / total_tokens)
    # Marginal cost: tokens the candidate adds beyond what members already cost.
    incremental = max(0, candidate.token_count - member_tokens)
    cost = min(1.0, incremental / max(1, token_budget))
    noise = max(0.0, 1.0 - density_value)
    # Extended attribution-risk features.
    member_count_norm = min(1.0, len(member_ids) / 8.0)
    values = [float(v) for v in member_scores.values()]
    value_sum = sum(values)
    entropy_norm = 0.0
    if len(values) > 1 and value_sum > 0:
        probs = [v / value_sum for v in values if v > 0]
        entropy = -sum(p * math.log(p) for p in probs)
        entropy_norm = min(1.0, entropy / math.log(len(values)))
    query_length_norm = min(1.0, len(query.split()) / 25.0)
    return MergeFeatures(
        relevance=relevance,
        coverage=min(1.0, coverage),
        coherence=coherence,
        density=density_value,
        noise=noise,
        cost=cost,
        query_factoid=float(query_type == QueryType.FACTOID),
        query_explanatory=float(query_type == QueryType.EXPLANATORY),
        query_comparative=float(query_type == QueryType.COMPARATIVE),
        query_global=float(query_type == QueryType.GLOBAL),
        member_count_norm=member_count_norm,
        member_score_entropy=entropy_norm,
        query_length_norm=query_length_norm,
    )


def features_for_candidate(
    hierarchy: Hierarchy,
    candidate_id: str,
    member_scores: Mapping[str, float],
    query_type: QueryType,
    token_budget: int,
    candidate_score: float,
    total_members: int,
    query: str = "",
) -> MergeFeatures:
    """Public feature extraction shared by the rollout harness and training."""
    return _features(
        hierarchy, candidate_id, member_scores, query_type,
        token_budget, candidate_score, total_members, query=query,
    )


def members_at_level(
    hierarchy: Hierarchy, candidate_id: str, member_level: Level
) -> list[str]:
    """Descendants of `candidate` that sit exactly at `member_level`."""
    output: list[str] = []
    frontier = list(hierarchy.node(candidate_id).child_ids)
    hops = 0
    while frontier and hops <= len(Level):
        nxt: list[str] = []
        for node_id in frontier:
            node = hierarchy.nodes.get(node_id)
            if node is None:
                continue
            if node.level == member_level:
                output.append(node_id)
            else:
                nxt.extend(node.child_ids)
        frontier = nxt
        hops += 1
    return output


def _descendant_ids(hierarchy: Hierarchy, candidate_id: str) -> set[str]:
    seen: set[str] = set()
    frontier = list(hierarchy.node(candidate_id).child_ids)
    hops = 0
    while frontier and hops <= len(Level):
        nxt: list[str] = []
        for node_id in frontier:
            if node_id in seen:
                continue
            seen.add(node_id)
            node = hierarchy.nodes.get(node_id)
            if node is not None:
                nxt.extend(node.child_ids)
        frontier = nxt
        hops += 1
    return seen


def effective_members(
    hierarchy: Hierarchy,
    candidate_id: str,
    member_scores: Mapping[str, float],
    member_level: Level,
) -> tuple[list[str], int]:
    """Retained evidence under `candidate`, expressed as merge members.

    Members are selected nodes at exactly `member_level`; selected nodes from
    shallower levels join as stand-ins for their slot whenever that slot was
    never consolidated (e.g. a leftover child next to an accepted sibling
    parent). Returns (member_ids, slot_count).
    """
    slots = members_at_level(hierarchy, candidate_id, member_level)
    slot_rank = level_rank(member_level)
    subtree = _descendant_ids(hierarchy, candidate_id)
    members = [slot for slot in slots if slot in member_scores]
    for node_id in member_scores:
        if node_id == candidate_id or node_id in members:
            continue
        node = hierarchy.nodes.get(node_id)
        if node is None or node_id not in subtree:
            continue
        if level_rank(node.level) < slot_rank:
            members.append(node_id)
    return members, len(slots)


def decide_candidates(
    query: str,
    query_type: QueryType,
    hierarchy: Hierarchy,
    reranker: Reranker,
    policy: AdaptiveMergePolicy,
    settings: Settings,
    candidates: Sequence[str],
    member_scores: Mapping[str, float],
    candidate_level: Level = Level.PARENT,
    member_level: Level = Level.CHILD,
    candidate_score_cache: dict[str, float] | None = None,
) -> list[MergeDecision]:
    """Generic utility comparison of a candidate node against its retained members.

    Works uniformly for parent-over-children, section-over-parents and
    document-over-sections; only levels differ.
    """
    cache = candidate_score_cache if candidate_score_cache is not None else {}
    missing_texts: list[str] = []
    missing_ids: list[str] = []
    for candidate_id in candidates:
        if candidate_id not in cache:
            missing_ids.append(candidate_id)
            missing_texts.append(hierarchy.node(candidate_id).text)
    if missing_ids:
        for candidate_id, score in zip(missing_ids, reranker.score(query, missing_texts)):
            cache[candidate_id] = float(score)

    decisions: list[MergeDecision] = []
    for candidate_id in candidates:
        candidate = hierarchy.node(candidate_id)
        member_ids_all, slot_count = effective_members(
            hierarchy, candidate_id, member_scores, member_level
        )
        member_hits = {
            member_id: float(member_scores[member_id]) for member_id in member_ids_all
        }
        if len(member_hits) < settings.min_child_hits:
            continue
        member_ids = tuple(member_hits)
        member_values = list(member_hits.values())
        u_members = sum(member_values) / len(member_values)
        candidate_score = cache[candidate_id]
        features = _features(
            hierarchy, candidate_id, member_hits, query_type,
            settings.context_token_budget, candidate_score, max(1, slot_count),
            query=query,
        )
        probability = policy.probability(features)
        u_parent = 0.5 * probability + 0.5 * candidate_score
        evidence_gain = max(0.0, 1.0 - features.coverage)
        member_token_sum = sum(hierarchy.node(m).token_count for m in member_ids)
        cost_delta_tokens = max(0, candidate.token_count - member_token_sum)
        cost_delta_norm = min(1.0, cost_delta_tokens / max(1, settings.context_token_budget))
        delta = (
            u_parent
            + policy.evidence_gain_weight * evidence_gain * u_parent
            - u_members
            - policy.cost_penalty * cost_delta_norm
        )
        best_member = max(member_values)
        force_accept = bool(getattr(policy, "force_accept", False))
        rolled_back = (
            candidate_score < settings.rollback_ratio * best_member
            and not force_accept
        )
        if policy.model is not None:
            # Learned attribution-risk policy: P(merge) from counterfactual
            # rollout rewards replaces the hand-tuned utility comparison.
            accepted = (
                probability >= policy.threshold and not rolled_back
            ) or force_accept
        else:
            delta = (
                u_parent
                + policy.evidence_gain_weight * evidence_gain * u_parent
                - u_members
                - policy.cost_penalty * cost_delta_norm
            )
            accepted = (delta >= policy.margin and not rolled_back) or force_accept
        decisions.append(
            MergeDecision(
                parent_id=candidate_id,
                accepted=accepted,
                probability=probability,
                utility=delta,
                features=features,
            child_ids=tuple(member_ids_all),
            selected_ids=(candidate_id,) if accepted else member_ids,
                reason=(
                    "rollback: candidate lost evidence specificity"
                    if rolled_back
                    else "candidate merged" if accepted else "members retained"
                ),
                rolled_back=rolled_back,
                level=candidate_level,
                member_level=member_level,
                parent_utility=u_parent,
                children_utility=u_members,
                evidence_gain=evidence_gain,
                cost_delta_tokens=cost_delta_tokens,
            )
        )
    return decisions


def decide_merges(
    query: str,
    query_type: QueryType,
    hierarchy: Hierarchy,
    hits: Sequence[Hit],
    reranker: Reranker,
    policy: AdaptiveMergePolicy,
    settings: Settings,
    candidate_score_cache: dict[str, float] | None = None,
) -> list[MergeDecision]:
    """Parent-level convenience wrapper over :func:`decide_candidates`."""
    # A real reranker may legitimately emit exactly 0.0, so fall back to the
    # retrieval score only when no hit carries an explicit reranker score.
    use_reranker = any(hit.reranker_score != 0.0 for hit in hits)
    member_scores = {
        hit.node_id: (hit.reranker_score if use_reranker else hit.score)
        for hit in hits
    }
    parents = [
        node.node_id
        for node in hierarchy.nodes.values()
        if node.level == Level.PARENT and any(c in member_scores for c in node.child_ids)
    ]
    return decide_candidates(
        query=query,
        query_type=query_type,
        hierarchy=hierarchy,
        reranker=reranker,
        policy=policy,
        settings=settings,
        candidates=parents,
        member_scores=member_scores,
        candidate_level=Level.PARENT,
        member_level=Level.CHILD,
        candidate_score_cache=candidate_score_cache,
    )


def ancestor_at_level(hierarchy: Hierarchy, node_id: str, target_level: Level) -> str | None:
    """Walk up the parent chain until a node at `target_level` is reached."""
    current = hierarchy.nodes.get(node_id)
    hops = 0
    while current is not None and hops <= len(Level):
        if current.level == target_level:
            return current.node_id
        if current.parent_id is None or current.parent_id not in hierarchy.nodes:
            return None
        current = hierarchy.nodes[current.parent_id]
        hops += 1
    return None


def group_members_by_ancestor(
    hierarchy: Hierarchy, selected_scores: Mapping[str, float], ancestor_level: Level
) -> dict[str, dict[str, float]]:
    """Group currently selected nodes by their ancestor at `ancestor_level`.

    Ancestor resolution follows real parent links, so a section candidate is
    compared against exactly its retained parents -- never against its own
    grandchildren.
    """
    grouped: dict[str, dict[str, float]] = defaultdict(dict)
    cache: dict[str, str | None] = {}
    for selected_id, score in selected_scores.items():
        if selected_id not in cache:
            cache[selected_id] = ancestor_at_level(hierarchy, selected_id, ancestor_level)
        ancestor = cache[selected_id]
        if ancestor is None:
            continue
        grouped[ancestor][selected_id] = float(score)
    return dict(grouped)
