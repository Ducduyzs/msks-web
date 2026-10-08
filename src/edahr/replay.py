"""Offline replay of claim verification from stored verification traces.

``verify_generation`` runs after generation, so its decisions depend only on
per-candidate scores that every benchmark row already stores (raw NLI
support/contradiction, lexical coverage, retrieval membership). Replaying the
selection rule over those traces evaluates verifier/citation policies without
any LLM or NLI call. ``replay_row`` with ``guard="legacy"`` and the run's own
settings must reproduce the stored selection exactly; scripts check this
before trusting any counterfactual policy.
"""

from __future__ import annotations

from dataclasses import dataclass

INF = float("inf")


@dataclass(frozen=True)
class ReplayPolicy:
    nli_support_threshold: float = 0.25
    nli_contradiction_threshold: float = 0.5
    lexical_support_min_coverage: float = 0.8
    evidence_margin: float = 0.05
    max_evidence_per_claim: int = 1
    # "legacy": lexical polarity/number check vetoed NLI entailment too;
    # "fixed": it only gates the lexical fallback (verification.py today).
    guard: str = "fixed"
    # Support required for a leaf outside the initial retrieval set.
    # None -> legacy sibling rule (threshold + sibling_delta when delta > 0);
    # INF -> cite retrieved leaves only (H4 strict); float -> calibrated (H5).
    expanded_threshold: float | None = None
    sibling_delta: float = 0.10


def _score(candidate: dict, policy: ReplayPolicy) -> tuple[float, bool]:
    raw = float(candidate["nli_support"])
    contradiction = float(candidate["nli_contradiction"])
    coverage = float(candidate["lexical_coverage"])
    contradicted = contradiction >= policy.nli_contradiction_threshold
    # The stored flag is contradiction OR lexical conflict; below the
    # contradiction threshold it is exactly the lexical-conflict outcome.
    lexical_conflict = bool(candidate["lexical_guard_blocked"]) and not contradicted
    fallback = (
        policy.lexical_support_min_coverage > 0.0
        and coverage >= policy.lexical_support_min_coverage
    )
    if policy.guard == "legacy":
        if contradicted or lexical_conflict:
            return 0.0, True
        score = raw
        if raw < policy.nli_support_threshold and fallback:
            score = max(score, coverage)
        return score, False
    if contradicted:
        return 0.0, True
    if raw < policy.nli_support_threshold and fallback:
        if lexical_conflict:
            return raw, True
        return max(raw, coverage), False
    return raw, False


def replay_claim(trace: dict, policy: ReplayPolicy, has_retrieval: bool = True) -> list[str]:
    """Selected leaf ids for one claim ([] = claim rejected).

    ``has_retrieval`` mirrors ``bool(retrieved_ids)`` in verify_generation: the
    sibling rule is skipped only for retrieval-free systems.
    """
    if trace.get("status") in {
        "invalid_or_missing_context_citation", "below_claim_confidence_threshold",
    }:
        return []
    scored: list[tuple[str, float, bool]] = []
    for candidate in trace.get("candidates") or ():
        score, _ = _score(candidate, policy)
        if score >= policy.nli_support_threshold:
            scored.append((candidate["child_id"], score, bool(candidate["initially_retrieved"])))
    if not scored:
        return []
    scored.sort(key=lambda item: item[1], reverse=True)
    if policy.expanded_threshold is None:
        if has_retrieval and policy.sibling_delta > 0.0:
            bar = policy.nli_support_threshold + policy.sibling_delta
            scored = [item for item in scored if item[2] or item[1] >= bar]
    elif has_retrieval:
        scored = [item for item in scored if item[2] or item[1] >= policy.expanded_threshold]
    if not scored:
        return []
    ambiguous = len(scored) > 1 and (scored[0][1] - scored[1][1]) < policy.evidence_margin
    limit = policy.max_evidence_per_claim
    kept = scored[:limit] if limit and limit > 0 else scored
    if ambiguous:
        kept = kept[:1]
    return [child_id for child_id, _, _ in kept]


def replay_row(row: dict, policy: ReplayPolicy) -> dict:
    """Accepted claim texts and cited leaves for one stored benchmark row."""
    claims: list[str] = []
    leaves: list[str] = []
    has_retrieval = bool(row.get("retrieved_child_ids")) and not row.get("retrieval_free")
    for trace in row.get("verification_trace") or ():
        selected = replay_claim(trace, policy, has_retrieval)
        if selected:
            claims.append(trace["claim_text"])
            leaves.extend(selected)
    retrieved = set(row.get("retrieved_child_ids") or ())
    gold = set(row.get("gold_child_ids") or ())
    cited = set(leaves)
    return {
        "claims": claims,
        "cited_leaves": sorted(cited),
        "harmful_drift": sorted((cited - retrieved) - gold),
        "rescued": sorted((cited - retrieved) & gold),
    }
