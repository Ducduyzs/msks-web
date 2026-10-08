"""Baseline systems B0-B6 and the shared benchmark harness.

Ladder (all share ingestion/hierarchy/generation/verification unless noted):

  B0  BM25 lexical child retrieval, flat context, no rerank
  B1  Dense-only FAISS child retrieval, flat context, no rerank
  B2  BM25 + dense fused with Reciprocal Rank Fusion, no rerank
  B3  Full neural fusion (dense+sparse+ColBERT) + cross-encoder rerank,
      flat context -- no hierarchical adaptation
  B4  Static hierarchical merging: parents always win when enough children
      are retrieved (non-adaptive hierarchy)
  B5_raptor_lightweight (alias B5_raptor, deprecated): TF-IDF/KMeans
      clustering with extractive centroid summaries; collapsed-tree
      multi-level retrieval fused with the shared leaf backend via RRF,
      flat context. NOT official/faithful RAPTOR.
  B5_raptor_faithful: semantic embeddings + UMAP/GMM/BIC clustering +
      abstractive LLM summaries + collapsed-tree cosine retrieval
      (see baselines_raptor.py). Per-paper trees (adaptation A1).
  B6_longrag_lightweight (alias B6_longrag, deprecated): section-level
      units ranked from shared-backend leaf scores, section-level long
      context with leaf-level verification. NOT official/faithful LongRAG.
  B6_longrag_faithful: semantic long-unit retriever + long-context reader
      (see baselines_longrag.py). Document-level units for QASPER.

The proposed system ("edahr") is the full adaptive pipeline.
"""

from __future__ import annotations

import json
import math
import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .attribution import attribution_metrics
from .config import Settings
from .context import assemble_context
from .evaluation import (
    aggregate,
    answer_exact_match,
    answer_token_f1,
    aurc,
    bootstrap_ci,
    citation_f1,
    citation_precision,
    citation_recall,
    e_aurc,
    evidence_span_recall,
    hit_rate_at_k,
    latency_stats,
    ndcg_at_k,
    precision_at_k,
    provenance_accuracy,
    recall_at_k,
    reciprocal_rank,
    qasper_answer_exact_match,
    qasper_answer_token_f1,
    qasper_evidence_f1,
    selective_accuracy_at_coverage,
)
from .hierarchy import HierarchyBuilder
from .interfaces import scoped_search
from .pipeline import AdaptiveHierarchicalPipeline, classify_query
from .policy import NeverMergePolicy, StaticMergePolicy, policies_from_settings
from .schemas import (
    Hierarchy,
    Hit,
    Level,
    ScientificDocument,
    level_rank,
)
from .verification import verify_generation


# ---------------------------------------------------------------------------
# Retrievers used only by baselines
# ---------------------------------------------------------------------------

class Bm25ChildRetriever:
    """Dependency-free BM25 over child passages (Okapi BM25, k1/b tunable)."""

    def __init__(self, hierarchy: Hierarchy, k1: float = 1.2, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.hierarchy = hierarchy
        self.node_ids = list(hierarchy.child_ids)
        self.doc_tokens: list[list[str]] = [
            hierarchy.node(node_id).text.lower().split() for node_id in self.node_ids
        ]
        self.doc_lengths = [len(tokens) for tokens in self.doc_tokens]
        self.avgdl = sum(self.doc_lengths) / max(1, len(self.doc_lengths))
        self.document_frequency: dict[str, int] = defaultdict(int)
        for tokens in self.doc_tokens:
            for term in set(tokens):
                self.document_frequency[term] += 1
        self.total_docs = len(self.node_ids)

    def _idf(self, term: str) -> float:
        frequency = self.document_frequency.get(term, 0)
        return math.log(1.0 + (self.total_docs - frequency + 0.5) / (frequency + 0.5))

    def search(self, query: str, k: int, source: str | None = None) -> list[Hit]:
        query_terms = query.lower().split()
        scores: list[float] = []
        for index, tokens in enumerate(self.doc_tokens):
            length_norm = self.k1 * (
                1.0 - self.b + self.b * self.doc_lengths[index] / max(1.0, self.avgdl)
            )
            counts: dict[str, int] = defaultdict(int)
            for token in tokens:
                counts[token] += 1
            score = 0.0
            for term in query_terms:
                tf = counts.get(term, 0)
                if tf:
                    score += self._idf(term) * tf * (self.k1 + 1.0) / (tf + length_norm)
            scores.append(score)
        allowed = [
            row for row, node_id in enumerate(self.node_ids)
            if source is None or self.hierarchy.node(node_id).source == source
        ]
        ranked = sorted(allowed, key=lambda i: scores[i], reverse=True)[:k]
        return [
            Hit(node_id=self.node_ids[row], score=scores[row], rank=rank)
            for rank, row in enumerate(ranked, start=1)
        ]


class RrfRetriever:
    """Reciprocal-rank fusion of several retrievers."""

    def __init__(self, retrievers: Sequence, rrf_k: int = 60):
        self.retrievers = list(retrievers)
        self.rrf_k = rrf_k
        self.hierarchy = next(
            (
                getattr(retriever, "hierarchy", None)
                for retriever in self.retrievers
                if getattr(retriever, "hierarchy", None) is not None
            ),
            None,
        )

    def search(self, query: str, k: int, source: str | None = None) -> list[Hit]:
        fused: dict[str, float] = defaultdict(float)
        first_scores: dict[str, float] = {}
        for retriever in self.retrievers:
            hits = (
                scoped_search(retriever, self.hierarchy, query, max(k, 100), source)
                if self.hierarchy is not None
                else retriever.search(query, max(k, 100))
            )
            for hit in hits:
                fused[hit.node_id] += 1.0 / (self.rrf_k + hit.rank)
                first_scores.setdefault(hit.node_id, hit.score)
        ordered = sorted(fused, key=fused.get, reverse=True)[:k]
        return [
            Hit(
                node_id=node_id,
                score=fused[node_id],
                rank=rank,
                dense_score=first_scores.get(node_id, 0.0),
            )
            for rank, node_id in enumerate(ordered, start=1)
        ]


# ---------------------------------------------------------------------------
# RAPTOR-style tree retrieval (B5) and LongRAG-style long units (B6)
# ---------------------------------------------------------------------------

def _word_tokens(text: str) -> list[str]:
    return str(text).lower().split()


def _split_sentences(text: str) -> list[str]:
    import re

    parts = re.split(r"(?<=[.!?])\s+", str(text).strip())
    return [part.strip() for part in parts if part.strip()]


@dataclass
class RaptorSummary:
    """One extractive summary node of the RAPTOR-style tree."""

    summary_id: str
    text: str
    member_child_ids: tuple[str, ...]
    sources: frozenset[str]
    depth: int


def build_raptor_tree(
    hierarchy: Hierarchy,
    cluster_size: int = 4,
    max_depth: int = 3,
    seed: int = 42,
    summary_sentences: int = 5,
    summary_token_cap: int = 220,
) -> list[RaptorSummary]:
    """Recursively cluster children and synthesize extractive summaries.

    Faithful lightweight instantiation of RAPTOR (Sarthi et al., ICLR 2024):
    bottom-up recursive embedding/clustering/summarization. Embeddings are
    TF-IDF vectors (stdlib/sklearn, deterministic) instead of neural
    embeddings so the baseline runs anywhere; clustering is KMeans with a
    contiguous-chunk fallback when sklearn is unavailable; summaries are
    extractive centroid sentences rather than LLM abstractions. Every
    summary retains its member leaf ids, so retrieval maps back to leaves
    and claim verification stays at leaf level.
    """
    child_ids = list(hierarchy.child_ids)
    if not child_ids:
        return []
    texts = [hierarchy.node(child_id).text for child_id in child_ids]
    try:
        from sklearn.cluster import KMeans
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        vectorizer = TfidfVectorizer(max_features=4096)
        matrix = vectorizer.fit_transform(texts)
        use_sklearn = True
    except Exception:
        use_sklearn = False  # type: ignore[assignment]

    summaries: list[RaptorSummary] = []
    current_items: list[tuple[str, tuple[str, ...]]] = [
        (text, (child_id,)) for text, child_id in zip(texts, child_ids)
    ]
    depth = 0
    while len(current_items) > max(1, cluster_size) and depth < max_depth:
        count = len(current_items)
        n_clusters = max(1, math.ceil(count / max(1, cluster_size)))
        n_clusters = min(n_clusters, count)
        labels: list[int]
        centroids = None
        if use_sklearn and n_clusters >= 2:
            try:
                from sklearn.cluster import KMeans
                from sklearn.feature_extraction.text import TfidfVectorizer
                from sklearn.metrics.pairwise import cosine_similarity  # noqa: F811

                item_texts = [text for text, _ in current_items]
                vectorizer = TfidfVectorizer(max_features=4096)
                item_matrix = vectorizer.fit_transform(item_texts)
                model = KMeans(n_clusters=n_clusters, n_init=10, random_state=seed + depth)
                labels = [int(label) for label in model.fit_predict(item_matrix)]
                centroids = model.cluster_centers_
            except Exception:
                labels = [index % n_clusters for index in range(count)]
        else:
            labels = [index % max(1, n_clusters) for index in range(count)]
        groups: dict[int, list[int]] = defaultdict(list)
        for index, label in enumerate(labels):
            groups[label].append(index)
        next_items: list[tuple[str, tuple[str, ...]]] = []
        for label in sorted(groups):
            member_indexes = groups[label]
            member_children: list[str] = []
            for index in member_indexes:
                member_children.extend(current_items[index][1])
            sentences: list[str] = []
            for index in member_indexes:
                sentences.extend(_split_sentences(current_items[index][0]))
            if centroids is not None and use_sklearn and sentences:
                try:
                    from sklearn.feature_extraction.text import TfidfVectorizer as _V
                    from sklearn.metrics.pairwise import cosine_similarity as _cos

                    sent_matrix = vectorizer.transform(sentences)
                    centroid = centroids[label].reshape(1, -1)
                    sims = _cos(sent_matrix, centroid).ravel()
                    ranked = sorted(
                        range(len(sentences)), key=lambda i: sims[i], reverse=True
                    )
                except Exception:
                    ranked = list(range(len(sentences)))
            else:
                ranked = sorted(
                    range(len(sentences)), key=lambda i: len(sentences[i].split())
                )
            picked: list[str] = []
            used = 0
            for index in ranked[: max(summary_sentences * 2, 1)]:
                sentence = sentences[index]
                tokens = len(sentence.split())
                if picked and used + tokens > summary_token_cap:
                    continue
                picked.append(sentence)
                used += tokens
                if len(picked) >= summary_sentences:
                    break
            summary_text = " ".join(picked) if picked else " ".join(sentences[:1])
            sources = frozenset(
                hierarchy.node(child_id).source for child_id in member_children
            )
            summary = RaptorSummary(
                summary_id=f"raptor-d{depth}-c{label}",
                text=summary_text,
                member_child_ids=tuple(member_children),
                sources=sources,
                depth=depth,
            )
            summaries.append(summary)
            next_items.append((summary_text, tuple(member_children)))
        current_items = next_items
        depth += 1
    return summaries


class _LexicalScorer:
    """Tiny BM25-like scorer over an arbitrary corpus (no extra dependencies)."""

    def __init__(self, documents: Sequence[str], k1: float = 1.2, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_tokens = [_word_tokens(document) for document in documents]
        self.doc_lengths = [len(tokens) for tokens in self.doc_tokens]
        self.avgdl = sum(self.doc_lengths) / max(1, len(self.doc_lengths))
        self.document_frequency: dict[str, int] = defaultdict(int)
        for tokens in self.doc_tokens:
            for term in set(tokens):
                self.document_frequency[term] += 1
        self.total_docs = len(self.doc_tokens)

    def score(self, query: str) -> list[float]:
        query_terms = _word_tokens(query)
        idfs = {
            term: math.log(
                1.0
                + (self.total_docs - self.document_frequency.get(term, 0) + 0.5)
                / (self.document_frequency.get(term, 0) + 0.5)
            )
            for term in set(query_terms)
        }
        scores: list[float] = []
        for index, tokens in enumerate(self.doc_tokens):
            length_norm = self.k1 * (
                1.0 - self.b + self.b * self.doc_lengths[index] / max(1.0, self.avgdl)
            )
            counts: dict[str, int] = defaultdict(int)
            for token in tokens:
                counts[token] += 1
            total = 0.0
            for term in query_terms:
                term_frequency = counts.get(term, 0)
                if term_frequency:
                    total += (
                        idfs[term]
                        * term_frequency
                        * (self.k1 + 1.0)
                        / (term_frequency + length_norm)
                    )
            scores.append(total)
        return scores


class RaptorRetriever:
    """Collapsed-tree retrieval over leaves plus recursive summary nodes.

    LIGHTWEIGHT variant (NOT official/faithful RAPTOR): TF-IDF clustering,
    extractive summaries, lexical summary scoring, RRF fusion.
    See ``baselines_raptor.py`` for the faithful implementation.

    Leaves are scored by the shared backend ``base_retriever`` (BM25 in
    offline runs, dense/neural index in production); summary nodes are
    scored lexically. Both rankings are fused with RRF and mapped back to
    child ids, mirroring RAPTOR's collapsed-tree query strategy while
    keeping citation scoring at leaf level.
    """

    mode = "lightweight"

    def __init__(
        self,
        hierarchy: Hierarchy,
        base_retriever=None,
        cluster_size: int = 4,
        max_depth: int = 3,
        seed: int = 42,
        rrf_k: int = 60,
    ):
        self.hierarchy = hierarchy
        self.base_retriever = base_retriever or Bm25ChildRetriever(hierarchy)
        self.summaries = build_raptor_tree(
            hierarchy, cluster_size=cluster_size, max_depth=max_depth, seed=seed
        )
        self.scorer = _LexicalScorer([summary.text for summary in self.summaries])
        self.rrf_k = rrf_k

    def _summary_child_ranking(
        self, query: str, k: int, source: str | None
    ) -> list[str]:
        if not self.summaries:
            return []
        scores = self.scorer.score(query)
        order = sorted(range(len(self.summaries)), key=lambda i: scores[i], reverse=True)
        ranking: list[str] = []
        seen: set[str] = set()
        for index in order:
            summary = self.summaries[index]
            if source is not None and source not in summary.sources:
                continue
            members = (
                [c for c in summary.member_child_ids
                 if self.hierarchy.node(c).source == source]
                if source is not None
                else list(summary.member_child_ids)
            )
            for child_id in members:
                if child_id not in seen:
                    seen.add(child_id)
                    ranking.append(child_id)
            if len(ranking) >= max(k, 100):
                break
        return ranking

    def search(self, query: str, k: int, source: str | None = None) -> list[Hit]:
        leaf_hits = scoped_search(
            self.base_retriever, self.hierarchy, query, max(k, 100), source
        )
        leaf_ranking = [hit.node_id for hit in leaf_hits]
        leaf_scores = {hit.node_id: hit.score for hit in leaf_hits}
        summary_ranking = self._summary_child_ranking(query, k, source)
        fused: dict[str, float] = defaultdict(float)
        for rank, node_id in enumerate(leaf_ranking, start=1):
            fused[node_id] += 1.0 / (self.rrf_k + rank)
        for rank, node_id in enumerate(summary_ranking, start=1):
            fused[node_id] += 1.0 / (self.rrf_k + rank)
        ordered = sorted(fused, key=fused.get, reverse=True)[:k]
        return [
            Hit(
                node_id=node_id,
                score=fused[node_id],
                rank=rank,
                dense_score=leaf_scores.get(node_id, 0.0),
            )
            for rank, node_id in enumerate(ordered, start=1)
        ]


class LongRagRetriever:
    """Long-unit retrieval: rank sections, order children inside winners.

    LIGHTWEIGHT variant (NOT official/faithful LongRAG): section-level units
    ranked by aggregated shared-backend BM25 leaf scores.
    See ``baselines_longrag.py`` for the faithful implementation.

    Instantiation of LongRAG (Jiang et al., 2024) for the paper-level
    setting: each section is one long retrieval unit (single-section
    documents fall back to two contiguous halves so the long/short
    distinction survives toy corpora). Unit relevance aggregates the
    shared-backend leaf scores (mean of the top-2 members), so the only
    difference versus flat retrieval is granularity; children of winning
    units come first, ordered by their own leaf score within each unit.
    """

    def __init__(self, hierarchy: Hierarchy, base_retriever=None):
        self.mode = "lightweight"
        self.hierarchy = hierarchy
        self.base_retriever = base_retriever or Bm25ChildRetriever(hierarchy)
        self.units: list[tuple[str, tuple[str, ...]]] = self._build_units(hierarchy)

    @staticmethod
    def _build_units(hierarchy: Hierarchy) -> list[tuple[str, tuple[str, ...]]]:
        by_section: dict[str, list[str]] = defaultdict(list)
        for child_id in hierarchy.child_ids:
            node = hierarchy.node(child_id)
            by_section[str(node.section_id or node.source)].append(child_id)
        units: list[tuple[str, tuple[str, ...]]] = []
        for section_key, members in by_section.items():
            if len(members) <= 2 and len(by_section) == 1:
                # Single-section toy document: two contiguous halves keep a
                # genuine long-vs-short granularity contrast.
                half = max(1, len(members) // 2)
                units.append((f"{section_key}#a", tuple(members[:half])))
                units.append((f"{section_key}#b", tuple(members[half:])))
            else:
                units.append((section_key, tuple(members)))
        return units

    def _unit_of(self, child_id: str) -> str:
        for unit_id, members in self.units:
            if child_id in members:
                return unit_id
        node = self.hierarchy.node(child_id)
        return str(node.section_id or node.source)

    def search(self, query: str, k: int, source: str | None = None) -> list[Hit]:
        leaf_hits = scoped_search(
            self.base_retriever, self.hierarchy, query, max(k, 100), source
        )
        leaf_scores = {hit.node_id: hit.score for hit in leaf_hits}
        unit_members: dict[str, list[str]] = defaultdict(list)
        for hit in leaf_hits:
            unit_members[self._unit_of(hit.node_id)].append(hit.node_id)

        def unit_score(unit_id: str) -> float:
            scores = sorted(
                (leaf_scores[c] for c in unit_members[unit_id]), reverse=True
            )[:2]
            return sum(scores) / len(scores) if scores else 0.0

        ranked_units = sorted(unit_members, key=unit_score, reverse=True)
        ordered: list[str] = []
        for unit_id in ranked_units:
            members = sorted(
                unit_members[unit_id], key=lambda c: leaf_scores[c], reverse=True
            )
            ordered.extend(members)
        ordered = ordered[:k]
        return [
            Hit(
                node_id=node_id,
                score=unit_score(self._unit_of(node_id)) + 1e-6 * leaf_scores[node_id],
                rank=rank,
                dense_score=leaf_scores[node_id],
            )
            for rank, node_id in enumerate(ordered, start=1)
        ]


# ---------------------------------------------------------------------------
# Dataset handling
# ---------------------------------------------------------------------------

def load_jsonl_dataset(path: str | Path) -> list[dict]:
    records: list[dict] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def split_by_paper(
    document_ids: Sequence[str],
    ratios: tuple[float, float, float] = (0.7, 0.1, 0.2),
    seed: int = 42,
) -> dict[str, list[str]]:
    """Paper-level train/calibration/test split (no leakage across splits)."""
    rng = random.Random(seed)
    ids = sorted(set(document_ids))
    rng.shuffle(ids)
    total = len(ids)
    train_end = round(total * ratios[0])
    calibration_end = train_end + round(total * ratios[1])
    return {
        "train": ids[:train_end],
        "calibration": ids[train_end:calibration_end],
        "test": ids[calibration_end:],
    }


def auto_label_gold_children(
    hierarchy: Hierarchy, record: dict, tau: float = 0.5
) -> tuple[set[str], list[str]]:
    """Resolve a record's gold evidence to child ids.

    Accepts explicit ``gold_child_ids`` and/or free-text ``gold_quotes``.
    Every child whose token-F1 with the quote reaches ``tau`` (or that contains
    the quote verbatim) is labelled gold, so a paragraph split across several
    chunks maps to all of them instead of an arbitrary best one -- otherwise
    citation precision is understated by construction.
    """
    gold_children = set(record.get("gold_child_ids") or ())
    gold_paragraph_ids = {
        str(paragraph_id)
        for paragraph_id in record.get("gold_paragraph_ids") or ()
    }
    if gold_paragraph_ids:
        gold_children.update(
            child_id for child_id in hierarchy.child_ids
            if gold_paragraph_ids.intersection(
                hierarchy.node(child_id).metadata.get("paragraph_ids") or ()
            )
        )
    allowed_sources = {str(key) for key in (record.get("gold_pages") or {})}
    if record.get("source"):
        allowed_sources.add(str(record["source"]))
    candidates = hierarchy.child_ids
    if allowed_sources:
        candidates = [
            child_id
            for child_id in hierarchy.child_ids
            if hierarchy.node(child_id).source in allowed_sources
        ]
    matched_quotes: list[str] = []
    for quote in record.get("gold_quotes") or ():
        normalized = " ".join(str(quote).lower().split())
        if not normalized:
            continue
        matches: list[str] = []
        for child_id in candidates:
            text = " ".join(hierarchy.node(child_id).text.lower().split())
            overlap = _quick_token_f1(normalized, text)
            if normalized in text:
                overlap = max(overlap, 1.0)
            if overlap >= tau:
                matches.append(child_id)
        if matches:
            gold_children.update(matches)
            matched_quotes.append(str(quote))
    return gold_children, matched_quotes or [str(q) for q in record.get("gold_quotes") or ()]


def children_for_paragraphs(hierarchy: Hierarchy, paragraph_ids: Iterable[str]) -> set[str]:
    """Resolve a QASPER paragraph set to every overlapping child leaf."""
    target = {str(paragraph_id) for paragraph_id in paragraph_ids}
    return {
        child_id for child_id in hierarchy.child_ids
        if target.intersection(hierarchy.node(child_id).metadata.get("paragraph_ids") or ())
    }


def _quick_token_f1(first: str, second: str) -> float:
    first_tokens, second_tokens = first.split(), second.split()
    if not first_tokens or not second_tokens:
        return 0.0
    second_counts = Counter(second_tokens)
    common = sum(
        min(count, second_counts.get(token, 0))
        for token, count in Counter(first_tokens).items()
    )
    if not common:
        return 0.0
    precision = common / len(first_tokens)
    recall = common / len(second_tokens)
    return 2 * precision * recall / (precision + recall)


# ---------------------------------------------------------------------------
# Benchmark execution
# ---------------------------------------------------------------------------

DEFAULT_CORRECTNESS = Callable[[float, float], float]


def default_correctness(answer_f1: float, citation_score: float) -> float:
    """A query is 'correct' when its answer overlaps gold AND cites gold evidence."""
    return float(answer_f1 >= 0.35 and citation_score > 0.0)


@dataclass
class BenchmarkRun:
    name: str
    rows: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def run_benchmark(
    name: str,
    pipeline: AdaptiveHierarchicalPipeline,
    records: Sequence[dict],
    ks: Sequence[int] = (3, 5, 10),
    correct_fn: DEFAULT_CORRECTNESS = default_correctness,
    seed: int = 42,
) -> BenchmarkRun:
    hierarchy = pipeline.hierarchy
    run = BenchmarkRun(name=name)
    for record in records:
        query = record["query"]
        gold_children, gold_quotes = auto_label_gold_children(hierarchy, record)
        gold_pages = {
            (str(source), int(page))
            for source, page in (record.get("gold_pages") or {}).items()
        }
        result = pipeline.answer(query, source=record.get("source"))
        ranked_ids = [hit.node_id for hit in result.hits]
        evidence_nodes = [evidence.node_id for evidence in result.evidence.values()]
        evidence_quotes = [evidence.quote for evidence in result.evidence.values()]
        predicted_paragraphs: dict[str, str] = {}
        for evidence in result.evidence.values():
            node = hierarchy.node(evidence.node_id)
            predicted_paragraphs.update(node.metadata.get("paragraph_texts") or {})
        provenance = [
            (evidence.source, evidence.page_start, evidence.page_end)
            for evidence in result.evidence.values()
        ]
        answer_text = " ".join(claim.text for claim in result.generation.claims).strip()
        mean_confidence = (
            sum(claim.confidence for claim in result.generation.claims)
            / len(result.generation.claims)
            if result.generation.claims
            else 0.0
        )
        evidence_node_set = {evidence.node_id for evidence in result.evidence.values()}
        retrieved_child_set = set(ranked_ids[: pipeline.settings.rerank_k])
        if getattr(pipeline, "retrieval_free", False):
            # No retrieval set: rescue/drift decomposition is undefined, so
            # treat every cited leaf as "kept" rather than as drift.
            retrieved_child_set = {evidence.node_id for evidence in result.evidence.values()}
        candidate_child_set = {
            child_id for block in result.context for child_id in block.evidence_ids
        }
        graded = {child_id: 1.0 for child_id in gold_children}
        citation_evaluable = bool(gold_children)
        is_qasper = "reference_evidence_sets" in record
        row: dict = {
            "query": query,
            "question_id": str(record.get("question_id") or ""),
            "source": record.get("source"),
            "citation_evaluable": citation_evaluable,
        }
        # Oracle/full-document systems have no ranking: retrieval metrics
        # are undefined (None), not zero.
        retrieval_free = bool(getattr(pipeline, "retrieval_free", False))
        row["retrieval_free"] = retrieval_free
        for k in ks:
            row[f"recall@{k}"] = None if retrieval_free else recall_at_k(ranked_ids, gold_children, k)
            row[f"precision@{k}"] = None if retrieval_free else precision_at_k(ranked_ids, gold_children, k)
            row[f"ndcg@{k}"] = None if retrieval_free else ndcg_at_k(ranked_ids, graded, k)
            row[f"hit_rate@{k}"] = None if retrieval_free else hit_rate_at_k(ranked_ids, gold_children, k)
        row["mrr"] = None if retrieval_free else reciprocal_rank(ranked_ids, gold_children)
        row["evidence_span_recall"] = (
            evidence_span_recall(evidence_quotes, gold_quotes)
            if gold_quotes
            else 0.0
        )
        if citation_evaluable:
            row["citation_precision"] = citation_precision(evidence_node_set, gold_children)
            row["citation_recall"] = citation_recall(evidence_node_set, gold_children)
            row["citation_f1"] = citation_f1(evidence_node_set, gold_children)
        else:
            # Undefined, not zero: zero would silently depress macro grounding
            # metrics for questions whose gold paragraph could not be mapped.
            row["citation_precision"] = None
            row["citation_recall"] = None
            row["citation_f1"] = None
        # Undefined without gold page labels (e.g. QASPER JSON has no pages);
        # 0.0 here previously reported a spurious zero for every system.
        row["provenance_accuracy"] = (
            provenance_accuracy(provenance, gold_pages) if gold_pages else None
        )
        gold_answer = str(record.get("answer") or record.get("gold_answer") or "")
        references = [str(answer) for answer in record.get("reference_answers") or [gold_answer]]
        if is_qasper:
            official_answer = answer_text or "Unanswerable"
            # Exact strings fed to the metric, so scripts/audit_evaluator.py
            # can re-score this row with AllenAI's official evaluator.
            row["predicted_answer"] = official_answer
            row["predicted_evidence_texts"] = list(predicted_paragraphs.values())
            row["answer_em"] = qasper_answer_exact_match(official_answer, references)
            row["answer_f1"] = qasper_answer_token_f1(official_answer, references)
            row["official_qasper_evidence_f1"] = qasper_evidence_f1(
                list(predicted_paragraphs.values()), record["reference_evidence_sets"]
            )
        else:
            row["answer_em"] = answer_exact_match(answer_text, gold_answer) if gold_answer else 0.0
            row["answer_f1"] = answer_token_f1(answer_text, gold_answer) if gold_answer else 0.0
        row["confidence"] = mean_confidence
        row["correct"] = (
            correct_fn(row["answer_f1"], float(row["citation_recall"]))
            if citation_evaluable else 0.0
        )
        row["context_tokens"] = float(result.metrics.get("context_tokens", 0.0))
        row["latency_ms"] = float(result.metrics.get("total_latency_ms", 0.0))
        # Per-query artifacts for failure decomposition and manual audit.
        row["generated_claim_count"] = int(
            result.metrics.get("generated_claims", len(result.generation.claims))
        )
        row["verified_claim_count"] = int(
            result.metrics.get("verified_claims", len(result.generation.claims))
        )
        row["verification_trace"] = list(result.verification_trace)
        row["generation_validation_errors"] = list(
            (result.raw_generation or result.generation).validation_errors
        )
        row["gold_child_ids"] = sorted(gold_children)
        row["gold_paragraph_ids"] = sorted(record.get("gold_paragraph_ids") or ())
        row["predicted_paragraph_ids"] = sorted(predicted_paragraphs)
        row["reference_paragraph_sets"] = list(record.get("reference_paragraph_sets") or ())
        row["evidence_node_ids"] = sorted(evidence_node_set)
        row["retrieved_child_ids"] = sorted(retrieved_child_set)
        row["candidate_child_ids"] = sorted(candidate_child_set)
        row["rescued_leaf_ids"] = sorted(
            (evidence_node_set - retrieved_child_set) & gold_children
        )
        row["harmful_drift_leaf_ids"] = sorted(
            (evidence_node_set - retrieved_child_set) - gold_children
        )
        row["kept_correct_leaf_ids"] = sorted(
            (evidence_node_set & retrieved_child_set) & gold_children
        )
        row["kept_wrong_leaf_ids"] = sorted(
            (evidence_node_set & retrieved_child_set) - gold_children
        )
        row["claim_evidence"] = [
            {
                "claim": evidence.claim_text,
                "node_id": evidence.node_id,
                "support_score": evidence.support_score,
                "context_id": evidence.context_id,
            }
            for evidence in result.evidence.values()
        ]
        run.rows.append(row)

    accuracies = [float(row["correct"]) for row in run.rows]
    confidences = [float(row["confidence"]) for row in run.rows]
    latencies = [float(row["latency_ms"]) for row in run.rows]
    macro = aggregate(run.rows)
    citation_scores = [
        float(row["citation_f1"])
        for row in run.rows
        if isinstance(row.get("citation_f1"), (int, float))
    ]
    ci_low, ci_high = bootstrap_ci(citation_scores, seed=seed)
    run.summary = {
        **macro,
        **{f"latency_{key}": value for key, value in latency_stats(latencies).items()},
        "aurc": aurc(accuracies, confidences) if accuracies else 0.0,
        "e_aurc": e_aurc(accuracies, confidences) if accuracies else 0.0,
        "selective_accuracy@80cov": (
            selective_accuracy_at_coverage(accuracies, confidences, 0.8)
            if accuracies else 0.0
        ),
        "citation_f1_ci_low": ci_low,
        "citation_f1_ci_high": ci_high,
        "num_queries": float(len(run.rows)),
        "citation_evaluable_queries": float(len(citation_scores)),
    }
    return run


def significance_vs_baseline(
    proposed: BenchmarkRun, baseline: BenchmarkRun, metric: str = "citation_f1", seed: int = 42
) -> float:
    from .evaluation import paired_bootstrap_test

    paired = _paired_metric_rows(proposed, baseline, metric)
    firsts, seconds = zip(*paired) if paired else ((0.0,), (0.0,))
    return paired_bootstrap_test(list(firsts), list(seconds), seed=seed)


def clustered_ci_vs_baseline(
    proposed: BenchmarkRun,
    baseline: BenchmarkRun,
    metric: str = "citation_f1",
    seed: int = 42,
) -> tuple[float, float]:
    """Paper-clustered CI for the paired proposed-minus-baseline difference."""
    from .evaluation import paired_cluster_bootstrap

    baseline_by_key = {_row_identity(row): row for row in baseline.rows}
    diffs: list[float] = []
    clusters: list[str] = []
    for row in proposed.rows:
        other = baseline_by_key.get(_row_identity(row))
        if other is None:
            continue
        first, second = row.get(metric), other.get(metric)
        if not isinstance(first, (int, float)) or not isinstance(second, (int, float)):
            continue
        diffs.append(float(first) - float(second))
        clusters.append(str(row.get("source") or "unknown-paper"))
    return paired_cluster_bootstrap(diffs, clusters, seed=seed)


def _row_identity(row: dict) -> tuple[str, ...]:
    question_id = str(row.get("question_id") or "")
    source = str(row.get("source") or "")
    if question_id:
        return source, question_id
    return source, str(row.get("query") or "")


def _paired_metric_rows(
    proposed: BenchmarkRun, baseline: BenchmarkRun, metric: str
) -> list[tuple[float, float]]:
    baseline_by_key = {_row_identity(row): row for row in baseline.rows}
    pairs: list[tuple[float, float]] = []
    for row in proposed.rows:
        other = baseline_by_key.get(_row_identity(row))
        if other is None:
            continue
        first, second = row.get(metric), other.get(metric)
        if isinstance(first, (int, float)) and isinstance(second, (int, float)):
            pairs.append((float(first), float(second)))
    return pairs


# ---------------------------------------------------------------------------
# Baseline construction from shared heavy components
# ---------------------------------------------------------------------------

class LongRagPipeline(AdaptiveHierarchicalPipeline):
    """LongRAG-style reader: section-level long context, leaf verification.

    LIGHTWEIGHT variant (NOT official/faithful LongRAG).

    Retrieval granularity is long (see :class:`LongRagRetriever`); the
    reader likewise consumes whole long units (sections) instead of short
    child blocks, mirroring LongRAG's long-retriever/long-reader balance.
    ``hits`` still carries reranked child hits so retrieval metrics stay
    comparable with every other baseline, and verification runs against
    descendant leaves -- never against the broad section text.
    """

    def answer(self, query: str, source: str | None = None):  # type: ignore[override]
        from .schemas import Result

        timings: dict[str, float] = {}
        query_type = classify_query(query)

        t0 = time.perf_counter()
        initial = scoped_search(
            self.retriever, self.hierarchy, query, self.settings.candidate_k, source
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

        member_scores = {hit.node_id: hit.reranker_score for hit in reranked}
        section_scores: dict[str, float] = defaultdict(float)
        section_counts: dict[str, int] = defaultdict(int)
        for node_id, score in member_scores.items():
            section_id = str(
                self.hierarchy.node(node_id).section_id
                or self.hierarchy.node(node_id).source
            )
            section_scores[section_id] = max(section_scores[section_id], float(score))
            section_counts[section_id] += 1
        timings["merge_ms"] = 0.0
        timings["expansion_ms"] = 0.0

        context = assemble_context(
            self.hierarchy, dict(section_scores), query_type, self.settings
        )

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
        metrics = {
            "retrieved_candidates": float(len(initial)),
            "reranked_candidates": float(len(reranked)),
            "context_blocks": float(len(context)),
            "context_tokens": float(sum(block.token_count for block in context)),
            "distinct_sources": float(len({block.source for block in context})),
            "merge_acceptance_rate": 0.0,
            "rollback_rate": 0.0,
            "expansion_decisions": 0.0,
            "max_level_used": float(level_rank(Level.SECTION)) if context else 0.0,
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
            decisions=(),
            metrics=metrics,
            expansion_trace=("longrag:section-level long units",),
            raw_generation=raw_generation,
            verification_trace=tuple(verification_trace),
        )

def build_documents(documents: list[ScientificDocument], settings: Settings) -> Hierarchy:
    return HierarchyBuilder(settings).build(documents)


def raptor_faithful_retriever(hierarchy: Hierarchy, settings: Settings):
    """Faithful RAPTOR collapsed-tree retriever (one cached tree per paper).

    Shared by ``B5_raptor_faithful`` and systems that put adaptive expansion
    on top of RAPTOR retrieval (``prior_raptor``).
    """
    from .baselines_raptor import (
        RaptorFaithfulConfig,
        RaptorFaithfulRetriever,
        build_paper_tree,
        make_summarizer,
    )

    faithful_cfg = getattr(settings, "raptor_faithful", None) or {}
    faithful_cfg = dict(faithful_cfg)
    if getattr(settings, "chunk_context", "none") != "none":
        faithful_cfg.setdefault("leaf_embedding_source", "embedding_text")
    config = RaptorFaithfulConfig(
        openai_api_key=settings.openai_api_key, **faithful_cfg
    )
    summarizer = make_summarizer(
        config, gemini_api_key=settings.gemini_api_key
    )
    sources = sorted(
        {hierarchy.node(child_id).source for child_id in hierarchy.child_ids}
    )
    trees = {}
    for source in sources:
        tree, _ = build_paper_tree(
            hierarchy, source, config, summarizer,
            cache_dir=config.cache_dir,
        )
        trees[source] = tree
    return RaptorFaithfulRetriever(hierarchy, trees, config)


def make_baseline_pipeline(
    name: str,
    hierarchy: Hierarchy,
    *,
    encoder=None,
    index_factory=None,
    reranker=None,
    generator=None,
    verifier=None,
    settings: Settings | None = None,
) -> AdaptiveHierarchicalPipeline:
    """Wire one of B0..B6 / 'edahr' from shared heavy components.

    ``index_factory(settings)`` must return a configured retrievable index
    (typically :class:`edahr.index.MultiRepresentationIndex`); BM25 is built
    locally without extra dependencies.

    ``B5_raptor`` / ``B6_longrag`` are deprecated aliases of the lightweight
    variants (a RuntimeWarning is emitted); use the explicit
    ``*_lightweight`` / ``*_faithful`` names in new experiments.
    """
    import warnings

    settings = settings or Settings()

    if name in ("B5_raptor", "B6_longrag"):
        warnings.warn(
            f"{name!r} is a deprecated alias of {name!r}_lightweight "
            "(lexical rút gọn, NOT official/faithful). Use the explicit "
            "'*_lightweight' or '*_faithful' baseline names.",
            RuntimeWarning, stacklevel=2,
        )
        name = f"{name}_lightweight"

    if name == "B0_bm25":
        variant = replace(settings, use_dense=False, use_sparse=False, use_colbert=False)
        pipeline_settings = replace(variant)
        retriever = Bm25ChildRetriever(hierarchy, settings.bm25_k1, settings.bm25_b)
        return AdaptiveHierarchicalPipeline(
            hierarchy=hierarchy, retriever=retriever, reranker=reranker,
            generator=generator, verifier=verifier, settings=pipeline_settings,
            policy=NeverMergePolicy(), rerank_enabled=False,
        )
    if name == "B1_dense":
        variant = replace(settings, use_dense=True, use_sparse=False, use_colbert=False)
        return AdaptiveHierarchicalPipeline(
            hierarchy=hierarchy, retriever=index_factory(variant), reranker=reranker,
            generator=generator, verifier=verifier, settings=variant,
            policy=NeverMergePolicy(), rerank_enabled=False,
        )
    if name == "B2_hybrid_rrf":
        variant = replace(
            settings,
            use_dense=True, use_sparse=False, use_colbert=False,
            fusion_mode="rrf",
        )
        bm25 = Bm25ChildRetriever(hierarchy, settings.bm25_k1, settings.bm25_b)
        dense_retriever = index_factory(variant)
        return AdaptiveHierarchicalPipeline(
            hierarchy=hierarchy,
            retriever=RrfRetriever([bm25, dense_retriever], settings.rrf_k),
            reranker=reranker,
            generator=generator,
            verifier=verifier,
            settings=replace(variant, expansion_max_depth=0),
            policy=NeverMergePolicy(),
            rerank_enabled=False,
        )
    if name == "B3_flat_neural":
        variant = replace(settings, expansion_max_depth=0)
        return AdaptiveHierarchicalPipeline(
            hierarchy=hierarchy, retriever=index_factory(variant), reranker=reranker,
            generator=generator, verifier=verifier, settings=variant,
            policy=NeverMergePolicy(), rerank_enabled=True,
        )
    if name == "B4_static_hierarchy":
        return AdaptiveHierarchicalPipeline(
            hierarchy=hierarchy, retriever=index_factory(replace(settings)),
            reranker=reranker, generator=generator, verifier=verifier,
            settings=settings, policy=StaticMergePolicy(), rerank_enabled=True,
        )
    if name == "B5_raptor_lightweight":
        base = index_factory(replace(settings, expansion_max_depth=0))
        retriever = RaptorRetriever(hierarchy, base_retriever=base)
        retriever.mode = "lightweight"
        variant = replace(settings, expansion_max_depth=0)
        return AdaptiveHierarchicalPipeline(
            hierarchy=hierarchy, retriever=retriever, reranker=reranker,
            generator=generator, verifier=verifier, settings=variant,
            policy=NeverMergePolicy(), rerank_enabled=True,
        )
    if name == "B5_raptor_faithful":
        retriever = raptor_faithful_retriever(hierarchy, settings)
        variant = replace(settings, expansion_max_depth=0)
        return AdaptiveHierarchicalPipeline(
            hierarchy=hierarchy, retriever=retriever, reranker=reranker,
            generator=generator, verifier=verifier, settings=variant,
            policy=NeverMergePolicy(), rerank_enabled=True,
        )
    if name == "B6_longrag_lightweight":
        base = index_factory(replace(settings, expansion_max_depth=0))
        retriever = LongRagRetriever(hierarchy, base_retriever=base)
        return LongRagPipeline(
            hierarchy=hierarchy, retriever=retriever, reranker=reranker,
            generator=generator, verifier=verifier, settings=settings,
            policy=NeverMergePolicy(), rerank_enabled=True,
        )
    if name == "B6_longrag_faithful":
        import hashlib as _hashlib

        from .baselines_longrag import (
            LongRagFaithfulConfig,
            LongRagFaithfulIndex,
            LongRagFaithfulRetriever,
            build_document_units,
        )

        faithful_cfg = dict(getattr(settings, "longrag_faithful", None) or {})
        faithful_cfg.setdefault("reader_provider", "shared-controlled")
        config = LongRagFaithfulConfig(**faithful_cfg)
        units = build_document_units(hierarchy)
        fingerprint = _hashlib.sha256(
            "|".join(
                [config.embedding_model, str(config.subchunk_tokens)]
                + [f"{u.unit_id}:{_hashlib.sha256(u.text.encode()).hexdigest()[:16]}"
                   for u in units]
            ).encode()
        ).hexdigest()[:16]
        cache_path = Path(config.cache_dir) / f"index-{fingerprint}.pkl"
        if cache_path.is_file():
            index = LongRagFaithfulIndex.load(cache_path)
            if [u.unit_id for u in index.units] != [u.unit_id for u in units]:
                index = LongRagFaithfulIndex(units, config)
                index.build()
                index.save(cache_path)
        else:
            index = LongRagFaithfulIndex(units, config)
            index.build()
            index.save(cache_path)
        retriever = LongRagFaithfulRetriever(hierarchy, index, config)
        # Controlled configuration: faithful semantic long-unit retrieval with
        # the shared generator/reader. Primary long-context LLM reader runs
        # are executed via scripts/run_faithful_smoke.py (Task 8/9).
        variant = replace(settings, expansion_max_depth=0)
        return AdaptiveHierarchicalPipeline(
            hierarchy=hierarchy, retriever=retriever, reranker=reranker,
            generator=generator, verifier=verifier, settings=variant,
            policy=NeverMergePolicy(), rerank_enabled=True,
        )
    if name == "edahr":
        parent_policy, section_policy = policies_from_settings(settings)
        return AdaptiveHierarchicalPipeline(
            hierarchy=hierarchy, retriever=index_factory(replace(settings)),
            reranker=reranker, generator=generator, verifier=verifier,
            settings=settings,
            parent_policy=parent_policy,
            section_policy=section_policy,
            rerank_enabled=True,
        )
    raise ValueError(f"Unknown baseline: {name}")


BASELINE_NAMES: tuple[str, ...] = (
    "B0_bm25", "B1_dense", "B2_hybrid_rrf", "B3_flat_neural",
    "B4_static_hierarchy", "B5_raptor_lightweight", "B5_raptor_faithful",
    "B6_longrag_lightweight", "B6_longrag_faithful", "edahr",
)
