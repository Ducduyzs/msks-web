from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import Settings
from .schemas import Hierarchy, Hit


def _minmax(values: dict[Any, float]) -> dict[Any, float]:
    if not values:
        return {}
    low, high = min(values.values()), max(values.values())
    if high - low < 1e-9:
        return {key: 1.0 for key in values}
    return {key: (value - low) / (high - low) for key, value in values.items()}


class MultiRepresentationIndex:
    """FAISS dense retrieval + BGE-M3 learned sparse + ColBERT late interaction.

    Representation flags and ``fusion_mode`` ("weighted" | "rrf") are read from
    settings so retrieval ablations (dense-only, no-ColBERT, BM25+dense RRF)
    reuse the exact same index class as the full system.
    """

    def __init__(self, hierarchy: Hierarchy, encoder: Any, settings: Settings):
        try:
            import faiss
            import numpy as np
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install faiss-cpu and numpy to build the index") from exc
        self.faiss = faiss
        self.np = np
        self.hierarchy = hierarchy
        self.encoder = encoder
        self.settings = settings
        self.node_ids = list(hierarchy.child_ids)
        representations = encoder.encode(
            [hierarchy.node(node_id).embedding_text for node_id in self.node_ids]
        )
        self.dense = np.asarray(representations["dense_vecs"], dtype="float32")
        faiss.normalize_L2(self.dense)
        self.dense_index = faiss.IndexFlatIP(self.dense.shape[1])
        self.dense_index.add(self.dense)
        self.sparse: list[dict[Any, float]] = representations["lexical_weights"]
        self.colbert = representations["colbert_vecs"]
        self.postings: dict[Any, list[tuple[int, float]]] = defaultdict(list)
        for row, weights in enumerate(self.sparse):
            for token_id, weight in weights.items():
                self.postings[token_id].append((row, float(weight)))

    # ------------------------------------------------------------------

    def _sparse_scores(self, query_weights: dict[Any, float]) -> dict[int, float]:
        scores: dict[int, float] = defaultdict(float)
        for token_id, query_weight in query_weights.items():
            for row, doc_weight in self.postings.get(token_id, []):
                scores[row] += float(query_weight) * doc_weight
        return dict(scores)

    def _colbert_score(self, query_vectors: Any, doc_vectors: Any) -> float:
        query = self.np.asarray(query_vectors, dtype="float32")
        document = self.np.asarray(doc_vectors, dtype="float32")
        if not query.size or not document.size:
            return 0.0
        similarities = query @ document.T
        return float(similarities.max(axis=1).mean())

    def _rrf_fuse(
        self,
        ranked_lists: list[list[int]],
        pool: set[int],
        k: int,
    ) -> dict[int, float]:
        scores = {row: 0.0 for row in pool}
        for ranking in ranked_lists:
            for rank, row in enumerate(ranking, start=1):
                if row in scores:
                    scores[row] += 1.0 / (k + rank)
        return scores

    def search(self, query: str, k: int, source: str | None = None) -> list[Hit]:
        settings = self.settings
        encoded = self.encoder.encode([query], batch_size=1)
        allowed_rows = [
            row for row, node_id in enumerate(self.node_ids)
            if source is None or self.hierarchy.node(node_id).source == source
        ]
        if not allowed_rows:
            return []
        allowed_set = set(allowed_rows)
        pool_size = min(len(allowed_rows), max(k, settings.candidate_k))
        dense_scores: dict[int, float] = {}
        sparse_all: dict[int, float] = {}
        colbert_raw: dict[int, float] = {}
        dense_ranking: list[int] = []
        sparse_ranking: list[int] = []

        if settings.use_dense:
            query_dense = self.np.asarray(encoded["dense_vecs"], dtype="float32")
            self.faiss.normalize_L2(query_dense)
            if source is None:
                values, rows = self.dense_index.search(query_dense, pool_size)
                pairs = [
                    (int(row), float(score))
                    for row, score in zip(rows[0], values[0])
                    if row >= 0
                ]
            else:
                scoped = self.dense[allowed_rows] @ query_dense[0]
                local_order = self.np.argsort(-scoped)[:pool_size]
                pairs = [
                    (allowed_rows[int(local)], float(scoped[int(local)]))
                    for local in local_order
                ]
            dense_scores = dict(pairs)
            dense_ranking = [row for row, _ in pairs]

        if settings.use_sparse:
            sparse_all = {
                row: score
                for row, score in self._sparse_scores(
                    encoded["lexical_weights"][0]
                ).items()
                if row in allowed_set
            }
            sparse_ranking = sorted(sparse_all, key=sparse_all.get, reverse=True)[:pool_size]

        candidates: set[int] = set(dense_scores) | set(sparse_ranking)
        if settings.use_colbert:
            colbert_raw = {
                row: self._colbert_score(encoded["colbert_vecs"][0], self.colbert[row])
                for row in candidates
            }
            colbert_scores = _minmax(colbert_raw)
        else:
            colbert_scores = {}

        if settings.fusion_mode == "rrf":
            fused = self._rrf_fuse(
                [dense_ranking, sparse_ranking], candidates, settings.rrf_k
            )
            if settings.use_colbert and colbert_raw:
                colbert_order = sorted(colbert_raw, key=colbert_raw.get, reverse=True)
                for rank, row in enumerate(colbert_order, start=1):
                    fused.setdefault(row, 0.0)
                    fused[row] += 1.0 / (settings.rrf_k + rank)
            dense_norm = {row: dense_scores.get(row, 0.0) for row in candidates}
            sparse_norm = {row: sparse_all.get(row, 0.0) for row in candidates}
        else:
            active_weights: list[float] = []
            if settings.use_dense:
                active_weights.append(settings.dense_weight)
            if settings.use_sparse:
                active_weights.append(settings.sparse_weight)
            if settings.use_colbert:
                active_weights.append(settings.colbert_weight)
            total_weight = sum(active_weights) or 1.0

            dense_norm = (
                _minmax({r: v for r, v in dense_scores.items()}) if settings.use_dense else {}
            )
            sparse_norm = (
                _minmax({row: sparse_all.get(row, 0.0) for row in candidates})
                if settings.use_sparse
                else {}
            )

            fused = {}
            for row in candidates:
                score = 0.0
                if settings.use_dense:
                    score += (settings.dense_weight / total_weight) * dense_norm.get(row, 0.0)
                if settings.use_sparse:
                    score += (settings.sparse_weight / total_weight) * sparse_norm.get(row, 0.0)
                if settings.use_colbert:
                    score += (settings.colbert_weight / total_weight) * colbert_scores.get(row, 0.0)
                fused[row] = score

        ordered = sorted(fused, key=fused.get, reverse=True)[:k]
        hits: list[Hit] = []
        for rank, row in enumerate(ordered, start=1):
            hits.append(
                Hit(
                    node_id=self.node_ids[row],
                    score=fused[row],
                    rank=rank,
                    dense_score=dense_norm.get(row, 0.0) if settings.use_dense else 0.0,
                    sparse_score=sparse_norm.get(row, 0.0) if settings.use_sparse else 0.0,
                    colbert_score=colbert_scores.get(row, 0.0),
                )
            )
        return hits

    def save_dense_index(self, path: str | Path) -> None:
        self.faiss.write_index(self.dense_index, str(path))
