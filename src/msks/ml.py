"""Model của v11, nạp lười một lần cho mỗi process worker (mục 4.3: một process sở hữu GPU).

BGE-M3 (dense + sparse), cross-encoder bge-reranker-v2-m3, SBERT multi-qa-mpnet (Agreement Ranking),
DeBERTa NLI (kiểm chứng). API process không import module này.
"""
from __future__ import annotations

import threading
from functools import cached_property
from typing import Sequence

import numpy as np

from .settings import get_settings

SPARSE_DIM = 250002  # kích thước từ vựng XLM-R của BGE-M3


class Models:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.lock = threading.Lock()  # GPU concurrency = 1 (mục 12.2)

    @property
    def device(self) -> str:
        if self.settings.device.startswith("cuda"):
            import torch

            if not torch.cuda.is_available():
                return "cpu"
        return self.settings.device

    @cached_property
    def encoder(self):
        from FlagEmbedding import BGEM3FlagModel

        return BGEM3FlagModel(self.settings.embedding_model, use_fp16=self.settings.use_fp16, devices=self.device)

    @cached_property
    def reranker(self):
        from edahr.models import BGEReranker

        return BGEReranker(self.settings.reranker_model, self.device, self.settings.use_fp16)

    @cached_property
    def sbert(self):
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer(self.settings.sbert_model, device=self.device)

    @cached_property
    def nli(self):
        from edahr.models import NliVerifier

        return NliVerifier(self.settings.nli_model, self.device)

    # ----------------------------------------------------------------- encode

    def encode(self, texts: Sequence[str]) -> tuple[np.ndarray, list[dict[int, float]]]:
        """Dense (đã chuẩn hóa) + lexical weights của BGE-M3."""
        with self.lock:
            out = self.encoder.encode(
                list(texts), batch_size=8, max_length=1024,
                return_dense=True, return_sparse=True, return_colbert_vecs=False,
            )
        dense = np.asarray(out["dense_vecs"], dtype=np.float32)
        sparse = [{int(k): float(v) for k, v in weights.items()} for weights in out["lexical_weights"]]
        return dense, sparse

    def sbert_encode(self, texts: Sequence[str]) -> np.ndarray:
        with self.lock:
            return np.asarray(
                self.sbert.encode(list(texts), normalize_embeddings=True, show_progress_bar=False, batch_size=32),
                dtype=np.float32,
            )

    def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        with self.lock:
            return self.reranker.score(query, list(texts))

    def nli_scores(self, claim: str, evidence: str) -> tuple[float, float]:
        with self.lock:
            return self.nli.score_details(claim, evidence)

    def revisions(self) -> dict[str, str]:
        s = self.settings
        return {
            "embedding": s.embedding_model,
            "reranker": s.reranker_model,
            "sbert": s.sbert_model,
            "nli": s.nli_model,
            "llm": f"{s.llm_provider}:{s.llm_model}",
        }


_models: Models | None = None


def get_models() -> Models:
    global _models
    if _models is None:
        _models = Models()
    return _models


class LockedVerifier:
    """Bọc NLI để edahr.verification gọi qua cùng khóa GPU."""

    def __init__(self, models: Models):
        self.models = models

    def support_score(self, claim: str, evidence: str) -> float:
        return self.models.nli_scores(claim, evidence)[0]

    def score_details(self, claim: str, evidence: str) -> tuple[float, float]:
        return self.models.nli_scores(claim, evidence)


def vector_literal(values: np.ndarray) -> str:
    return "[" + ",".join(f"{float(v):.6g}" for v in values) + "]"


def sparse_literal(weights: dict[int, float]) -> str:
    """sparsevec của pgvector dùng chỉ số bắt đầu từ 1."""
    items = ",".join(f"{token + 1}:{weight:.6g}" for token, weight in sorted(weights.items()) if weight > 0)
    return "{" + items + "}/" + str(SPARSE_DIM)
