from __future__ import annotations

from .config import Settings
from .interfaces import Verifier
from .text import claim_coverage
from .schemas import (
    Claim,
    ContextBlock,
    Evidence,
    Generation,
    Hierarchy,
    Level,
)


def _lexical_conflict(claim: str, evidence: str) -> bool:
    """Reject lexical rescue when polarity or comparable numbers disagree."""
    claim_words = {word.casefold() for word in claim.split()}
    evidence_words = {word.casefold() for word in evidence.split()}
    negations = {"no", "not", "never", "neither", "without", "none", "cannot", "can't"}
    if bool(claim_words & negations) != bool(evidence_words & negations):
        return True
    import re

    numbers = re.compile(r"(?<!\w)\d+(?:\.\d+)?%?(?!\w)")
    claim_numbers = set(numbers.findall(claim))
    evidence_numbers = set(numbers.findall(evidence))
    return bool(claim_numbers and evidence_numbers and not (claim_numbers & evidence_numbers))


def _nli_scores(verifier: Verifier, claim: str, evidence: str) -> tuple[float, float]:
    details = getattr(verifier, "score_details", None)
    if callable(details):
        support, contradiction = details(claim, evidence)
        return float(support), float(contradiction)
    return float(verifier.support_score(claim, evidence)), 0.0


def _candidate_children(
    cited_blocks: list[ContextBlock], hierarchy: Hierarchy
) -> list[str]:
    """Children actually reachable from the cited context, in reading order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for block in cited_blocks:
        for evidence_id in block.evidence_ids:
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            node = hierarchy.nodes.get(evidence_id)
            if node is not None and node.level == Level.CHILD:
                ordered.append(evidence_id)
    return ordered


def verify_generation(
    generation: Generation,
    context: list[ContextBlock],
    hierarchy: Hierarchy,
    verifier: Verifier,
    settings: Settings,
    claim_supports: list[tuple[str, float]] | None = None,
    retrieved_ids: set[str] | None = None,
    verification_trace: list[dict] | None = None,
) -> tuple[Generation, dict[str, Evidence], dict[str, float]]:
    """Child-level NLI verification with leaf-level attribution gating.

    For every generated claim we run entailment against each descendant child
    of the cited context -- never against the whole parent/context blob.
    Selection then enforces precision on top of recall:

    * siblings outside the initially retrieved set must clear
      ``nli_support_threshold + sibling_threshold_delta``;
    * at most ``max_evidence_per_claim`` children become citations (top-1 by
      default);
    * when ``top1 - top2 < evidence_margin`` the choice is ambiguous, so only
      the top child is kept and the claim is flagged in metrics.

    When ``claim_supports`` is a list, ``(claim_text, best_support)`` pairs are
    appended for every claim reaching the NLI stage (pre-threshold best score).
    """
    context_by_id = {block.context_id: block for block in context}
    accepted_claims: list[Claim] = []
    evidence: dict[str, Evidence] = {}
    support_values: list[float] = []
    nli_calls = 0
    rejected_no_child = 0
    ambiguous_claims = 0
    sibling_filtered = 0
    rejected_invalid_citation = 0
    rejected_low_confidence = 0
    rejected_base_support = 0
    rejected_sibling_guard = 0
    rejected_safety_guard = 0

    def add_evidence(claim: Claim, child_id: str, score: float, block: ContextBlock) -> str:
        node = hierarchy.node(child_id)
        evidence_id = f"E{len(evidence) + 1}"
        evidence[evidence_id] = Evidence(
            evidence_id=evidence_id,
            node_id=node.node_id,
            source=node.source,
            page_start=node.page_start,
            page_end=node.page_end,
            quote=node.text,
            support_score=round(float(score), 4),
            char_start=node.char_start,
            char_end=node.char_end,
            claim_text=claim.text,
            context_id=block.context_id,
            confidence=node.confidence,
        )
        return evidence_id

    for claim_index, claim in enumerate(generation.claims):
        cited = [context_by_id[cid] for cid in claim.citations if cid in context_by_id]
        trace = {
            "claim_index": claim_index,
            "claim_text": claim.text,
            "claim_confidence": float(claim.confidence),
            "cited_context_ids": list(claim.citations),
            "invalid_context_ids": [cid for cid in claim.citations if cid not in context_by_id],
            "candidates": [],
            "selected_child_ids": [],
        }
        if not cited:
            rejected_invalid_citation += 1
            trace["status"] = "invalid_or_missing_context_citation"
            if verification_trace is not None:
                verification_trace.append(trace)
            continue
        if claim.confidence < settings.claim_confidence_threshold:
            rejected_low_confidence += 1
            trace["status"] = "below_claim_confidence_threshold"
            if verification_trace is not None:
                verification_trace.append(trace)
            continue
        candidates = _candidate_children(cited, hierarchy)[: settings.max_children_per_claim]
        if not candidates:
            rejected_no_child += 1
            rejected_base_support += 1
            trace["status"] = "no_reachable_child"
            if claim_supports is not None:
                claim_supports.append((claim.text, 0.0))
            if verification_trace is not None:
                verification_trace.append(trace)
            continue
        scored: list[tuple[str, float, ContextBlock]] = []
        best_support = 0.0
        for child_id in candidates:
            child_text = hierarchy.node(child_id).text
            raw_score, contradiction = _nli_scores(verifier, claim.text, child_text)
            nli_calls += 1
            coverage = float(claim_coverage(claim.text, child_text))
            score = raw_score
            # Strong NLI contradiction always vetoes. The lexical polarity /
            # number check only gates the lexical *fallback*: applied to NLI
            # entailment it vetoed any claim whose 220-token leaf contained a
            # stray "not" or an unrelated number (~25% of claims, mostly gold).
            safety_blocked = contradiction >= settings.nli_contradiction_threshold
            if safety_blocked:
                score = 0.0
            elif raw_score < settings.nli_support_threshold:
                # Deterministic fallback: near-verbatim restatements (including
                # numeral paraphrases like "six" vs "N = 6") that the NLI
                # checkpoint scores as neutral still count as supported.
                if (
                    settings.lexical_support_min_coverage > 0.0
                    and coverage >= settings.lexical_support_min_coverage
                ):
                    if _lexical_conflict(claim.text, child_text):
                        safety_blocked = True
                    else:
                        score = max(score, coverage)
            best_support = max(best_support, float(score))
            origin = next(block for block in cited if child_id in block.evidence_ids)
            passed_base = score >= settings.nli_support_threshold
            trace["candidates"].append({
                "child_id": child_id,
                "origin_context_id": origin.context_id,
                "nli_support": round(raw_score, 6),
                "nli_contradiction": round(contradiction, 6),
                "lexical_coverage": round(coverage, 6),
                "effective_support": round(float(score), 6),
                "lexical_guard_blocked": safety_blocked,
                "initially_retrieved": bool(retrieved_ids and child_id in retrieved_ids),
                "passed_base_threshold": bool(passed_base),
                "passed_sibling_guard": False,
                "selected": False,
            })
            if safety_blocked:
                rejected_safety_guard += 1
            if passed_base:
                scored.append((child_id, float(score), origin))
        if not scored:
            rejected_no_child += 1
            rejected_base_support += 1
            trace["status"] = "below_support_threshold"
            if claim_supports is not None:
                claim_supports.append((claim.text, round(best_support, 4)))
            if verification_trace is not None:
                verification_trace.append(trace)
            continue
        if claim_supports is not None:
            claim_supports.append((claim.text, round(best_support, 4)))
        scored.sort(key=lambda item: item[1], reverse=True)
        expanded_bar = getattr(settings, "expanded_citation_threshold", None)
        if retrieved_ids and (expanded_bar is not None or settings.sibling_threshold_delta > 0.0):
            bar = (
                expanded_bar if expanded_bar is not None
                else settings.nli_support_threshold + settings.sibling_threshold_delta
            )
            before = len(scored)
            scored = [
                entry for entry in scored
                if entry[0] in retrieved_ids or entry[1] >= bar
            ]
            sibling_filtered += before - len(scored)
        surviving = {child_id for child_id, _, _ in scored}
        for item in trace["candidates"]:
            item["passed_sibling_guard"] = (
                bool(item["passed_base_threshold"]) and item["child_id"] in surviving
            )
        if not scored:
            rejected_no_child += 1
            rejected_sibling_guard += 1
            trace["status"] = "sibling_guard_rejected_all"
            if verification_trace is not None:
                verification_trace.append(trace)
            continue
        ambiguous = (
            len(scored) > 1
            and (scored[0][1] - scored[1][1]) < settings.evidence_margin
        )
        limit = settings.max_evidence_per_claim
        kept = scored[:limit] if limit and limit > 0 else scored
        if ambiguous:
            kept = kept[:1]
            ambiguous_claims += 1
        selected_ids = {child_id for child_id, _, _ in kept}
        trace["selected_child_ids"] = sorted(selected_ids)
        trace["status"] = "accepted"
        for item in trace["candidates"]:
            item["selected"] = item["child_id"] in selected_ids
        if verification_trace is not None:
            verification_trace.append(trace)
        support_values.extend(score for _, score, _ in kept)
        verified_citations = tuple(
            add_evidence(claim, child_id, score, block)
            for child_id, score, block in kept
        )
        accepted_claims.append(
            Claim(text=claim.text, citations=verified_citations, confidence=claim.confidence)
        )

    verified = Generation(
        answerable=generation.answerable and bool(accepted_claims),
        claims=tuple(accepted_claims),
        reason=(
            generation.reason if accepted_claims
            else "No generated claim had a child passage pass NLI verification."
        ),
    )
    metrics = {
        "generated_claims": float(len(generation.claims)),
        "verified_claims": float(len(accepted_claims)),
        "claim_precision_proxy": len(accepted_claims) / max(1, len(generation.claims)),
        "mean_nli_support": sum(support_values) / max(1, len(support_values)),
        "child_nli_calls": float(nli_calls),
        "claims_rejected_no_child_support": float(rejected_no_child),
        "verified_evidence_children": float(len(evidence)),
        "ambiguous_claims": float(ambiguous_claims),
        "sibling_filtered_children": float(sibling_filtered),
        "claims_rejected_invalid_citation": float(rejected_invalid_citation),
        "claims_rejected_low_confidence": float(rejected_low_confidence),
        "claims_rejected_base_support": float(rejected_base_support),
        "claims_rejected_sibling_guard": float(rejected_sibling_guard),
        "candidate_safety_guard_rejections": float(rejected_safety_guard),
    }
    return verified, evidence, metrics
