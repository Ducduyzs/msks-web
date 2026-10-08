from __future__ import annotations

import re
import time
from dataclasses import replace

from .config import Settings
from .attribution import attribution_metrics
from .context import assemble_context
from .expansion import MAX_LEVEL_BY_QUERY_TYPE, expand_selection
from .interfaces import Generator, Reranker, Retriever, Verifier, scoped_search
from .policy import AdaptiveMergePolicy, decide_merges
from .schemas import Hierarchy, Hit, Level, QueryType, Result, level_rank
from .verification import verify_generation


_COMPARATIVE = ("compare", "versus", " vs ", "difference", "better",
                "so sánh", "khác nhau", "hơn hay")
_GLOBAL = ("overall", "summarize", "main contribution", "key findings",
           "toàn bộ", "tổng quan", "kết luận", "đóng góp chính")
_EXPLANATORY = ("why", "how", "explain", "mechanism", "rationale",
                "tại sao", "như thế nào", "giải thích")


def classify_query(query: str) -> QueryType:
    """Deterministic cue-based classifier (learned classifier can be swapped in)."""
    lowered = f" {query.lower()} "
    if any(term in lowered for term in _COMPARATIVE):
        return QueryType.COMPARATIVE
    if any(term in lowered for term in _GLOBAL):
        return QueryType.GLOBAL
    if any(term in lowered for term in _EXPLANATORY) or re.search(
        r"\b(what|which)\b.*\b(result|lead|cause|effect)\b", lowered
    ):
        return QueryType.EXPLANATORY
    return QueryType.FACTOID


class AdaptiveHierarchicalPipeline:
    def __init__(
        self,
        hierarchy: Hierarchy,
        retriever: Retriever,
        reranker: Reranker,
        generator: Generator,
        verifier: Verifier,
        settings: Settings | None = None,
        policy: AdaptiveMergePolicy | None = None,
        parent_policy: AdaptiveMergePolicy | None = None,
        section_policy: AdaptiveMergePolicy | None = None,
        rerank_enabled: bool = True,
    ):
        self.hierarchy = hierarchy
        self.retriever = retriever
        self.reranker = reranker
        self.generator = generator
        self.verifier = verifier
        self.settings = settings or Settings()
        self.rerank_enabled = rerank_enabled
        fallback_policy = policy or AdaptiveMergePolicy(
            threshold=self.settings.merge_threshold,
            margin=self.settings.merge_margin,
            evidence_gain_weight=self.settings.evidence_gain_weight,
            cost_penalty=self.settings.cost_penalty,
        )
        self.parent_policy = parent_policy or fallback_policy
        self.section_policy = section_policy or self.parent_policy
        # Compatibility for callers and serialized experiment helpers.
        self.policy = self.parent_policy

    # ------------------------------------------------------------------

    def answer(self, query: str, source: str | None = None) -> Result:
        timings: dict[str, float] = {}
        query_type = classify_query(query)

        t0 = time.perf_counter()
        initial = scoped_search(
            self.retriever,
            self.hierarchy,
            query,
            self.settings.candidate_k,
            source,
        )
        timings["retrieval_ms"] = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        rerank_pool = initial[: self.settings.rerank_k]
        if self.rerank_enabled and rerank_pool:
            scores = self.reranker.score(
                query, [self.hierarchy.node(hit.node_id).text for hit in rerank_pool]
            )
        else:
            scores = [hit.score for hit in rerank_pool]
        reranked = [
            replace(hit, reranker_score=float(score))
            for hit, score in zip(rerank_pool, scores)
        ]
        reranked.sort(key=lambda hit: hit.reranker_score, reverse=True)
        reranked = [replace(hit, rank=rank) for rank, hit in enumerate(reranked, start=1)]
        timings["rerank_ms"] = (time.perf_counter() - t0) * 1000

        score_cache: dict[str, float] = {
            hit.node_id: hit.reranker_score for hit in reranked
        }
        member_scores = {hit.node_id: hit.reranker_score for hit in reranked}

        t0 = time.perf_counter()
        parent_decisions = decide_merges(
            query=query,
            query_type=query_type,
            hierarchy=self.hierarchy,
            hits=reranked,
            reranker=self.reranker,
            policy=self.parent_policy,
            settings=self.settings,
            candidate_score_cache=score_cache,
        )
        selected = dict(member_scores)
        for decision in parent_decisions:
            if not decision.accepted:
                continue
            for member_id in decision.child_ids:
                selected.pop(member_id, None)
            selected[decision.parent_id] = score_cache.get(
                decision.parent_id, decision.parent_utility
            )
        timings["merge_ms"] = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        expanded, expansion_decisions, trace = expand_selection(
            query=query,
            query_type=query_type,
            hierarchy=self.hierarchy,
            reranker=self.reranker,
            policy=self.section_policy,
            settings=self.settings,
            selected_scores=selected,
            score_cache=score_cache,
            start_rank=level_rank(Level.PARENT),
        )
        timings["expansion_ms"] = (time.perf_counter() - t0) * 1000

        decisions = tuple([*parent_decisions, *expansion_decisions])

        context = assemble_context(self.hierarchy, expanded, query_type, self.settings)

        t0 = time.perf_counter()
        raw_generation = self.generator.generate(query, context)
        timings["generation_ms"] = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        claim_supports: list[tuple[str, float]] = []
        verification_trace: list[dict] = []
        generation, evidence, verification_metrics = verify_generation(
            raw_generation, context, self.hierarchy, self.verifier,
            self.settings, claim_supports=claim_supports,
            retrieved_ids={hit.node_id for hit in reranked},
            verification_trace=verification_trace,
        )
        timings["verification_ms"] = (time.perf_counter() - t0) * 1000

        total_ms = sum(timings.values())
        accepted_all = [d for d in decisions if d.accepted]
        metrics = {
            "retrieved_candidates": float(len(initial)),
            "reranked_candidates": float(len(reranked)),
            "context_blocks": float(len(context)),
            "context_tokens": float(sum(block.token_count for block in context)),
            "distinct_sources": float(len({block.source for block in context})),
            "merge_acceptance_rate": (
                sum(d.accepted for d in decisions) / max(1, len(decisions))
            ),
            "rollback_rate": sum(d.rolled_back for d in decisions) / max(1, len(decisions)),
            "expansion_decisions": float(len(expansion_decisions)),
            "max_level_used": float(
                max((level_rank(d.level) for d in accepted_all), default=0)
            ),
            "ceiling_level": float(level_rank(MAX_LEVEL_BY_QUERY_TYPE[query_type])),
            "total_latency_ms": total_ms,
            "generation_contract_rejections": float(
                len(raw_generation.validation_errors)
            ),
            **{f"latency_{key}": value for key, value in timings.items()},
            **verification_metrics,
            **attribution_metrics(
                claim_supports,
                len(raw_generation.claims),
                len(generation.claims),
                self.settings.nli_support_threshold,
            ),
        }
        return Result(
            query=query,
            query_type=query_type,
            generation=generation,
            context=tuple(context),
            evidence=evidence,
            hits=tuple(reranked),
            decisions=decisions,
            metrics=metrics,
            expansion_trace=tuple(trace),
            raw_generation=raw_generation,
            verification_trace=tuple(verification_trace),
        )
