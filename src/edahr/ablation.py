"""Component-wise ablation framework.

Each :class:`AblationConfig` toggles exactly one axis of the system
(representations, fusion, reranker, merge controller, expansion, NLI
verification) so a benchmark run yields the standard ablation table:

    full | -sparse | -colbert | dense-only | rrf-fusion | -rerank |
    flat (no merge) | static merge | -expansion | -verification

Heavy models are shared across variants; only cheap wiring changes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace as dc_replace
from typing import Any, Sequence

from .baselines import BenchmarkRun, run_benchmark
from .config import Settings
from .pipeline import AdaptiveHierarchicalPipeline
from .policy import AdaptiveMergePolicy, NeverMergePolicy, StaticMergePolicy


@dataclass(frozen=True)
class AblationConfig:
    name: str
    use_dense: bool = True
    use_sparse: bool = True
    use_colbert: bool = True
    fusion_mode: str = "weighted"
    rerank: bool = True
    merge: str = "adaptive"          # adaptive | static | none
    expansion: bool = True
    verification: bool = True


def default_grid() -> list[AblationConfig]:
    return [
        AblationConfig("full"),
        AblationConfig("-sparse", use_sparse=False),
        AblationConfig("-colbert", use_colbert=False),
        AblationConfig("dense-only", use_sparse=False, use_colbert=False),
        AblationConfig("rrf-fusion", fusion_mode="rrf"),
        AblationConfig("-rerank", rerank=False),
        AblationConfig("flat-no-merge", merge="none"),
        AblationConfig("static-merge", merge="static"),
        AblationConfig("-expansion", expansion=False),
        AblationConfig("-verification", verification=False),
    ]


class _PassThroughVerifier:
    """Disables NLI filtering while keeping the claim/evidence plumbing."""

    def support_score(self, claim: str, evidence: str) -> float:
        return 1.0


def variant_settings(base: Settings, config: AblationConfig) -> Settings:
    return dc_replace(
        base,
        use_dense=config.use_dense,
        use_sparse=config.use_sparse,
        use_colbert=config.use_colbert,
        fusion_mode=config.fusion_mode,
        expansion_max_depth=base.expansion_max_depth if config.expansion else 0,
    )


def build_variant(
    config: AblationConfig,
    components: dict[str, Any],
    settings: Settings,
) -> AdaptiveHierarchicalPipeline:
    """Wire a pipeline variant from shared heavy components.

    ``components`` keys: hierarchy, index_factory(settings)->retriever,
    reranker, generator, verifier.
    """
    variant = variant_settings(settings, config)
    if config.merge == "adaptive":
        policy = AdaptiveMergePolicy(
            threshold=variant.merge_threshold,
            margin=variant.merge_margin,
            evidence_gain_weight=variant.evidence_gain_weight,
            cost_penalty=variant.cost_penalty,
        )
    elif config.merge == "static":
        policy = StaticMergePolicy()
    else:
        policy = NeverMergePolicy()
    verifier = components["verifier"] if config.verification else _PassThroughVerifier()
    return AdaptiveHierarchicalPipeline(
        hierarchy=components["hierarchy"],
        retriever=components["index_factory"](variant),
        reranker=components["reranker"],
        generator=components["generator"],
        verifier=verifier,
        settings=variant,
        policy=policy,
        rerank_enabled=config.rerank,
    )


def run_ablation(
    records: Sequence[dict],
    components: dict[str, Any],
    settings: Settings | None = None,
    grid: Sequence[AblationConfig] | None = None,
    ks: Sequence[int] = (3, 5, 10),
    seed: int | None = None,
) -> list[BenchmarkRun]:
    settings = settings or Settings()
    seed = seed if seed is not None else settings.seed
    results: list[BenchmarkRun] = []
    for config in (grid or default_grid()):
        pipeline = build_variant(config, components, settings)
        results.append(
            run_benchmark(config.name, pipeline, records, ks=ks, seed=seed)
        )
    return results


def ablation_table(runs: Sequence[BenchmarkRun], metrics: Sequence[str] = (
    "recall@5", "ndcg@5", "citation_f1", "evidence_span_recall",
    "answer_f1", "provenance_accuracy",
)) -> list[dict]:
    """Rows ready to be rendered as the paper's ablation table."""
    table: list[dict] = []
    for run in runs:
        row: dict[str, Any] = {"system": run.name}
        for metric in metrics:
            value = run.summary.get(metric)
            row[metric] = round(value, 4) if isinstance(value, float) else value
        row["p95_ms"] = round(run.summary.get("latency_p95_ms", 0.0), 1)
        table.append(row)
    return table
