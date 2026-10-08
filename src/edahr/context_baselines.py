"""Retrieval-free reference systems: gold-evidence oracle and full-document reader.

Both bypass retrieval, reranking and merge policies and hand a fixed node set
straight to context assembly, then run the *same* generator and claim-level
verifier as every other system. Retrieval metrics (recall@k, MRR, nDCG) are
undefined for them; ``retrieval_free`` tells the benchmark to report None.

- ``oracle_evidence``: context = the gold QASPER evidence leaves of the
  question, in document order. An upper bound on generation/attribution given
  perfect retrieval, not a deployable system.
- ``full_document``: context = every section of the question's paper, in
  document order, under a long-context token budget. The long-context
  baseline; sections are non-overlapping, so no text is duplicated, and
  citations still resolve to leaves through each section's evidence children.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Mapping, Sequence

from .context import assemble_context
from .pipeline import AdaptiveHierarchicalPipeline, classify_query
from .schemas import Hierarchy, Level, Result
from .verification import verify_generation


class FixedContextPipeline(AdaptiveHierarchicalPipeline):
    """Generate + verify over a caller-defined node set; no retrieval stage."""

    retrieval_free = True

    def select_nodes(self, query: str, source: str | None) -> list[str]:
        raise NotImplementedError

    def _context_settings(self, node_count: int):
        # Never let the default top-k cap silently drop fixed context nodes.
        return replace(self.settings, final_context_k=max(1, node_count))

    def answer(self, query: str, source: str | None = None) -> Result:
        query_type = classify_query(query)
        node_ids = self.select_nodes(query, source)
        # Descending utility in document order: assemble_context sorts by
        # utility, so this keeps the original reading order of the paper.
        scores = {
            node_id: float(len(node_ids) - position)
            for position, node_id in enumerate(node_ids)
        }
        context = assemble_context(
            self.hierarchy, scores, query_type, self._context_settings(len(node_ids))
        )
        timings: dict[str, float] = {}
        t0 = time.perf_counter()
        raw_generation = self.generator.generate(query, context)
        timings["generation_ms"] = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        claim_supports: list[tuple[str, float]] = []
        verification_trace: list[dict] = []
        # retrieved_ids=None: the sibling guard is defined relative to a
        # retrieval set, which these systems do not have.
        generation, evidence, verification_metrics = verify_generation(
            raw_generation, context, self.hierarchy, self.verifier,
            self.settings, claim_supports=claim_supports,
            retrieved_ids=None, verification_trace=verification_trace,
        )
        timings["verification_ms"] = (time.perf_counter() - t0) * 1000

        metrics = {
            "selected_nodes": float(len(node_ids)),
            "context_blocks": float(len(context)),
            "context_tokens": float(sum(block.token_count for block in context)),
            "context_truncated_blocks": float(sum(block.truncated for block in context)),
            "context_dropped_nodes": float(max(0, len(node_ids) - len(context))),
            "total_latency_ms": sum(timings.values()),
            "generation_contract_rejections": float(len(raw_generation.validation_errors)),
            "generated_claims": float(len(raw_generation.claims)),
            "verified_claims": float(len(generation.claims)),
            **{f"latency_{key}": value for key, value in timings.items()},
            **verification_metrics,
        }
        return Result(
            query=query,
            query_type=query_type,
            generation=generation,
            context=tuple(context),
            evidence=evidence,
            hits=(),
            decisions=(),
            metrics=metrics,
            expansion_trace=(),
            raw_generation=raw_generation,
            verification_trace=tuple(verification_trace),
        )


def _document_order(hierarchy: Hierarchy, node_ids: Sequence[str]) -> list[str]:
    order = {node_id: index for index, node_id in enumerate(hierarchy.nodes)}
    return sorted(dict.fromkeys(node_ids), key=lambda node_id: order[node_id])


class OracleEvidencePipeline(FixedContextPipeline):
    """Context = gold evidence leaves, keyed by (source, query)."""

    def __init__(self, *args, gold_children: Mapping[tuple[str, str], Sequence[str]], **kwargs):
        super().__init__(*args, **kwargs)
        self.gold_children = {key: tuple(value) for key, value in gold_children.items()}

    def select_nodes(self, query: str, source: str | None) -> list[str]:
        key = (str(source or ""), query)
        if key not in self.gold_children:
            raise KeyError(f"no gold evidence registered for question {key!r}")
        return _document_order(self.hierarchy, self.gold_children[key])


class FullDocumentPipeline(FixedContextPipeline):
    """Context = every section of the question's paper, long-context budget."""

    def __init__(self, *args, token_budget: int = 100_000, **kwargs):
        super().__init__(*args, **kwargs)
        self.token_budget = int(token_budget)

    def _context_settings(self, node_count: int):
        return replace(
            super()._context_settings(node_count),
            context_token_budget=self.token_budget,
            # Near-duplicate removal is a retrieval-context heuristic; the
            # full-document reader must see the paper as written.
            context_dedup_threshold=1.01,
        )

    def select_nodes(self, query: str, source: str | None) -> list[str]:
        if not source:
            raise ValueError("full_document requires a source-scoped question")
        sections = [
            node_id for node_id, node in self.hierarchy.nodes.items()
            if node.level == Level.SECTION and node.source == source
        ]
        if not sections:
            raise KeyError(f"no sections for source {source!r}")
        return sections
