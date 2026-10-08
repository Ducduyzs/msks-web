from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Level(str, Enum):
    DOCUMENT = "document"
    SECTION = "section"
    PARENT = "parent"
    CHILD = "child"


LEVEL_ORDER: tuple[Level, ...] = (Level.CHILD, Level.PARENT, Level.SECTION, Level.DOCUMENT)


def level_rank(level: Level) -> int:
    return LEVEL_ORDER.index(level)


class QueryType(str, Enum):
    FACTOID = "factoid"
    EXPLANATORY = "explanatory"
    COMPARATIVE = "comparative_multi_hop"
    GLOBAL = "global_synthesis"


@dataclass(frozen=True)
class DocumentSection:
    title: str
    text: str
    page_start: int = 1
    page_end: int = 1
    section_type: str = "document"
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScientificDocument:
    document_id: str
    source: str
    sections: tuple[DocumentSection, ...]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Node:
    node_id: str
    level: Level
    document_id: str
    source: str
    text: str
    embedding_text: str
    page_start: int
    page_end: int
    section_id: str | None = None
    section_title: str = "Document"
    section_type: str = "document"
    parent_id: str | None = None
    child_ids: tuple[str, ...] = ()
    evidence_child_ids: tuple[str, ...] = ()
    position: int = 0
    token_count: int = 0
    char_start: int = 0
    char_end: int = 0
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def replaced(self, **changes: Any) -> "Node":
        return Node(**{**self.__dict__, **changes})


@dataclass(frozen=True)
class Hierarchy:
    nodes: dict[str, Node]
    child_ids: tuple[str, ...]

    def node(self, node_id: str) -> Node:
        return self.nodes[node_id]

    def descendants(self, node_id: str) -> tuple[str, ...]:
        return self.nodes[node_id].evidence_child_ids or (node_id,)


@dataclass(frozen=True)
class Hit:
    node_id: str
    score: float
    rank: int
    dense_score: float = 0.0
    sparse_score: float = 0.0
    colbert_score: float = 0.0
    reranker_score: float = 0.0


@dataclass(frozen=True)
class MergeFeatures:
    relevance: float
    coverage: float
    coherence: float
    density: float
    noise: float
    cost: float
    query_factoid: float
    query_explanatory: float
    query_comparative: float
    query_global: float
    # Extended attribution-risk features (appended so 10-field positional
    # construction stays valid; checkpoints are dim-sensitive either way).
    member_count_norm: float = 0.0
    member_score_entropy: float = 0.0
    query_length_norm: float = 0.0

    def vector(self) -> list[float]:
        return list(asdict(self).values())


@dataclass(frozen=True)
class MergeDecision:
    parent_id: str
    accepted: bool
    probability: float
    utility: float
    features: MergeFeatures
    child_ids: tuple[str, ...]
    selected_ids: tuple[str, ...]
    reason: str
    rolled_back: bool = False
    level: Level = Level.PARENT
    member_level: Level = Level.CHILD
    parent_utility: float = 0.0
    children_utility: float = 0.0
    evidence_gain: float = 0.0
    cost_delta_tokens: int = 0


@dataclass(frozen=True)
class ContextBlock:
    context_id: str
    node_id: str
    level: Level
    text: str
    source: str
    page_start: int
    page_end: int
    evidence_ids: tuple[str, ...]
    utility: float
    token_count: int
    char_start: int = 0
    char_end: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class Claim:
    text: str
    citations: tuple[str, ...]
    confidence: float


@dataclass(frozen=True)
class Generation:
    answerable: bool
    claims: tuple[Claim, ...] = ()
    reason: str = ""
    # Provider responses are retained even when they violate the citation
    # contract so benchmark telemetry cannot mistake rejection for compliance.
    validation_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    node_id: str
    source: str
    page_start: int
    page_end: int
    quote: str
    support_score: float
    char_start: int = 0
    char_end: int = 0
    claim_text: str = ""
    context_id: str = ""
    confidence: float = 1.0


@dataclass(frozen=True)
class Result:
    query: str
    query_type: QueryType
    generation: Generation
    context: tuple[ContextBlock, ...]
    evidence: dict[str, Evidence]
    hits: tuple[Hit, ...]
    decisions: tuple[MergeDecision, ...]
    metrics: dict[str, float]
    expansion_trace: tuple[str, ...] = ()
    raw_generation: Generation | None = None
    verification_trace: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
