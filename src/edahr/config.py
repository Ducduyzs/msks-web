from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    embedding_model: str = "BAAI/bge-m3"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    nli_model: str = "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli"
    llm_model: str = "gpt-4o-mini"
    llm_provider: str = "openai"  # "openai" | "gemini" | "antigravity"
    antigravity_agent: str = "antigravity-preview-05-2026"
    antigravity_max_total_tokens: int = 20000
    device: str = "cuda"
    use_fp16: bool = True
    child_target_tokens: int = 220
    child_overlap_sentences: int = 1
    children_per_parent: int = 4
    parent_overlap_children: int = 1
    candidate_k: int = 80
    rerank_k: int = 24
    final_context_k: int = 8
    context_token_budget: int = 7000
    dense_weight: float = 0.40
    sparse_weight: float = 0.25
    colbert_weight: float = 0.35
    rrf_k: int = 60
    fusion_mode: str = "weighted"  # "weighted" | "rrf"
    use_dense: bool = True
    use_sparse: bool = True
    use_colbert: bool = True
    # Adaptive merging: utility comparison between a parent and its children.
    merge_threshold: float = 0.56          # legacy monitoring threshold
    merge_margin: float = 0.04             # U(parent) must beat U(children)+margin
    rollback_ratio: float = 0.78
    min_child_hits: int = 2
    evidence_gain_weight: float = 0.35     # weight of incremental evidence gain
    cost_penalty: float = 0.50             # penalty per unit incremental token cost
    # Learned gates are independent: child->parent and parent->section/document.
    parent_policy_checkpoint: str | None = None
    section_policy_checkpoint: str | None = None
    enable_parent_expansion: bool = True
    enable_section_expansion: bool = True
    policy_version: str = "prior"
    # Iterative expansion child -> parent -> section -> document.
    expansion_max_depth: int = 3
    expansion_epsilon: float = 0.02        # stop when total gain below this
    expansion_headroom: float = 1.15       # projected budget headroom multiplier
    # Verification: child-level NLI instead of block-level.
    # Threshold calibrated on arXiv claim/evidence pairs: TPR .956 @ FPR .048.
    nli_support_threshold: float = 0.25
    nli_contradiction_threshold: float = 0.50
    claim_confidence_threshold: float = 0.55
    max_children_per_claim: int = 10
    # Leaf evidence selector: recall via expansion, precision via selection.
    max_evidence_per_claim: int = 1      # 0 keeps every child above threshold
    evidence_margin: float = 0.05        # gap below this => ambiguous -> top-1 only
    sibling_threshold_delta: float = 0.10  # extra bar for leaves outside retrieval
    # H4/H5: support a leaf outside the initial retrieval set needs to be cited.
    # None keeps the sibling rule above; a very large value (e.g. 2.0) cites
    # retrieved leaves only; scripts/replay_policies.py calibrates it with
    # conformal risk control (expected harmful drift <= alpha).
    expanded_citation_threshold: float | None = None
    # Deterministic lexical fallback when strict NLI rejects near-verbatim claims.
    lexical_support_min_coverage: float = 0.8
    # Retrieval-side chunk context (edahr.contextual): "none" | "title" | "llm".
    # "llm" reads contexts cached by scripts/contextualize_chunks.py.
    chunk_context: str = "none"
    chunk_context_path: str | None = None
    chunk_context_model: str | None = None
    # Context assembly guardrails.
    context_dedup_threshold: float = 0.85
    max_source_share: float = 0.65
    comparative_min_sources: int = 2
    knapsack_token_bucket: int = 64
    # Baselines / benchmarking.
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    seed: int = 42
    gemini_api_key: str | None = None
    openai_api_key: str | None = None
    # Faithful external baselines (B5_raptor_faithful / B6_longrag_faithful).
    # Dicts so model/version/hyperparameter choices stay in config files and
    # run provenance; see RaptorFaithfulConfig / LongRagFaithfulConfig.
    raptor_faithful: dict | None = None
    longrag_faithful: dict | None = None

    @classmethod
    def from_json(cls, path: str | Path) -> "Settings":
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        known = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in payload.items() if key in known})

    def to_dict(self) -> dict:
        return asdict(self)

