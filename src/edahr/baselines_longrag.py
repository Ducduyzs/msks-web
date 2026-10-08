"""Faithful LongRAG baseline (B6_longrag_faithful).

Reimplementation of LongRAG (Jiang–Ma–Chen, arXiv:2406.15319;
https://github.com/TIGER-AI-Lab/LongRAG, MIT):

- long retrieval units: one unit per paper (the paper's own Qasper setting:
  "each individual document as a single (long) unit", Table 5);
- semantic long retriever: ``BAAI/bge-large-en-v1.5`` embeddings with the
  paper's approximation ``sim(q,g) ≈ max_{512-token g'⊆g} E_Q(q)^T E_C(g')``,
  FAISS/numpy inner-product search, **no re-ranking**;
- long-context reader: concat of top-k units fed to a long-context LLM
  (Gemini-1.5-Pro / GPT-4o per paper; model recorded in metadata);
- unit→paragraph/leaf mapping preserves QASPER paragraph provenance and
  leaf attribution scoring.

Documented adaptations for this repo (see analysis/official_baseline_sources.md):
B1. Corpus = frozen-manifest papers (not Wikipedia); grouping step is trivial
    because Qasper itself uses document-level units.
B2. Controlled runs may substitute the shared repo generator for the LLM
    reader to isolate retriever quality; the substitution is recorded per run
    (``reader: "shared-controlled"`` vs ``reader: "<model-id>"``).
B3. Token budget: primary reader input = concat of top-k units (paper regime
    ~30K tokens); the exact token count is logged per query. Repo budget
    constants apply only to controlled runs.

Primary mode never uses BM25/leaf-score aggregation for unit ranking: a
missing embedding backend raises RuntimeError naming the component.
"""

from __future__ import annotations

import hashlib
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .schemas import Hierarchy, Hit

OFFICIAL_EMBEDDING_MODEL = "BAAI/bge-large-en-v1.5"
OFFICIAL_READERS = ("gemini-1.5-pro", "gpt-4o")
OFFICIAL_SUBCHUNK_TOKENS = 512
OFFICIAL_QASPER_TOP_K = 2


@dataclass(frozen=True)
class LongRagFaithfulConfig:
    embedding_model: str = OFFICIAL_EMBEDDING_MODEL
    subchunk_tokens: int = OFFICIAL_SUBCHUNK_TOKENS
    top_k: int = OFFICIAL_QASPER_TOP_K
    reader_provider: str = "gemini"  # "gemini" | "openai" | "shared-controlled"
    reader_model: str = "gemini-1.5-pro"
    reader_api_key: str | None = None
    reader_max_tokens: int = 2048
    cache_dir: str = "artifacts/baselines/longrag/index"
    device: str = "cuda"
    batch_size: int = 32


@dataclass
class LongRagUnit:
    unit_id: str  # == paper source for QASPER document-level units
    source: str
    text: str
    # (char_start, char_end, paragraph_id, paragraph_text) in unit coordinates.
    paragraphs: tuple[tuple[int, int, str, str], ...] = ()
    member_child_ids: tuple[str, ...] = ()


def _stable_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _split_words(text: str, size: int) -> list[str]:
    words = text.split()
    return [" ".join(words[i:i + size]) for i in range(0, len(words), size)] or [""]


def build_document_units(hierarchy: Hierarchy) -> list[LongRagUnit]:
    """One long retrieval unit per paper (official Qasper setting)."""
    by_source: dict[str, list[str]] = {}
    for child_id in hierarchy.child_ids:
        by_source.setdefault(hierarchy.node(child_id).source, []).append(child_id)
    units: list[LongRagUnit] = []
    for source, members in sorted(by_source.items()):
        # Order members by document position for a coherent long unit.
        members = sorted(
            members,
            key=lambda c: (
                hierarchy.node(c).section_id or "",
                hierarchy.node(c).position,
            ),
        )
        texts = [hierarchy.node(child_id).text for child_id in members]
        unit_text = "\n\n".join(texts)
        paragraphs: list[tuple[int, int, str, str]] = []
        cursor = 0
        seen: set[str] = set()
        for child_id in members:
            node = hierarchy.node(child_id)
            child_text = node.text
            start = unit_text.find(child_text, cursor)
            if start < 0:
                start = cursor
            end = start + len(child_text)
            for paragraph_id, paragraph_text in (
                node.metadata.get("paragraph_texts") or {}
            ).items():
                if paragraph_id in seen:
                    continue
                seen.add(paragraph_id)
                paragraphs.append((start, end, str(paragraph_id), str(paragraph_text)))
            cursor = end + 2
        units.append(
            LongRagUnit(
                unit_id=source, source=source, text=unit_text,
                paragraphs=tuple(paragraphs), member_child_ids=tuple(members),
            )
        )
    return units


def _embed_texts(
    texts: Sequence[str],
    model_name: str,
    device: str,
    batch_size: int,
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
) -> list[list[float]]:
    if embed_fn is not None:
        return [list(map(float, row)) for row in embed_fn(list(texts))]
    try:
        from FlagEmbedding import FlagModel
    except Exception as exc:
        raise RuntimeError(
            "LongRAG faithful requires 'FlagEmbedding' for the "
            f"{model_name} dense retriever. No BM25 fallback exists in "
            "primary mode."
        ) from exc
    model = _cached_flag_model(model_name, device)
    vectors: list[list[float]] = []
    texts = list(texts)
    for index in range(0, len(texts), batch_size):
        batch = texts[index:index + batch_size]
        encoded = model.encode(batch)
        vectors.extend([list(map(float, row)) for row in encoded])
    return vectors


_FLAG_MODEL_CACHE: dict[tuple[str, str], object] = {}


def _cached_flag_model(model_name: str, device: str):
    key = (model_name, device)
    if key not in _FLAG_MODEL_CACHE:
        try:
            from FlagEmbedding import FlagModel

            _FLAG_MODEL_CACHE[key] = FlagModel(
                model_name, query_instruction_for_retrieval="",
                use_fp16=(device == "cuda"),
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not load embedding model '{model_name}'. Check network "
                "access to huggingface.co or pre-download the weights."
            ) from exc
    return _FLAG_MODEL_CACHE[key]


class LongRagFaithfulIndex:
    """Semantic long-unit index with max-subchunk scoring (paper §3.1)."""

    mode = "faithful"

    def __init__(
        self,
        units: Sequence[LongRagUnit],
        config: LongRagFaithfulConfig,
        embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    ):
        self.units = list(units)
        self.config = config
        self._embed_fn = embed_fn
        self._subchunks: list[list[str]] = []
        self._embeddings: list[list[list[float]]] = []  # per unit: subchunk vectors
        self.build_time_s = 0.0

    def build(self) -> dict:
        t0 = time.perf_counter()
        for unit in self.units:
            chunks = _split_words(unit.text, self.config.subchunk_tokens)
            self._subchunks.append(chunks)
            self._embeddings.append(
                _embed_texts(
                    chunks, self.config.embedding_model, self.config.device,
                    self.config.batch_size, self._embed_fn,
                )
            )
        self.build_time_s = time.perf_counter() - t0
        return {
            "units": len(self.units),
            "subchunks": sum(len(c) for c in self._subchunks),
            "build_time_s": self.build_time_s,
            "embedding_model": self.config.embedding_model,
        }

    def score_units(self, query: str) -> list[tuple[str, float]]:
        import numpy as np

        query_vector = np.asarray(
            _embed_texts(
                [query], self.config.embedding_model, self.config.device, 1,
                self._embed_fn,
            )[0],
            dtype=np.float64,
        )
        scored: list[tuple[str, float]] = []
        for unit, embeddings in zip(self.units, self._embeddings):
            matrix = np.asarray(embeddings, dtype=np.float64)
            best = float((matrix @ query_vector).max()) if len(matrix) else 0.0
            scored.append((unit.unit_id, best))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored

    def save(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb") as handle:
            pickle.dump(
                {
                    "units": self.units, "subchunks": self._subchunks,
                    "embeddings": self._embeddings,
                    "config": self.config, "build_time_s": self.build_time_s,
                },
                handle,
            )
        return out

    @classmethod
    def load(cls, path: str | Path) -> "LongRagFaithfulIndex":
        with Path(path).open("rb") as handle:
            payload = pickle.load(handle)
        index = cls(payload["units"], payload["config"])
        index._subchunks = payload["subchunks"]
        index._embeddings = payload["embeddings"]
        index.build_time_s = payload.get("build_time_s", 0.0)
        return index


class LongRagFaithfulRetriever:
    """Semantic long-unit retriever returning leaf Hits for harness parity."""

    mode = "faithful"

    def __init__(
        self,
        hierarchy: Hierarchy,
        index: LongRagFaithfulIndex,
        config: LongRagFaithfulConfig,
        embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    ):
        self.hierarchy = hierarchy
        self.index = index
        self.config = config
        self._embed_fn = embed_fn or index._embed_fn
        self._units = {unit.unit_id: unit for unit in index.units}

    def index_metadata(self) -> dict:
        return {
            "baseline": "B6_longrag_faithful",
            "embedding_model": self.config.embedding_model,
            "subchunk_tokens": self.config.subchunk_tokens,
            "top_k": self.config.top_k,
            "reader": (
                "shared-controlled"
                if self.config.reader_provider == "shared-controlled"
                else f"{self.config.reader_provider}:{self.config.reader_model}"
            ),
            "units": len(self._units),
            "index_build_time_s": self.index.build_time_s,
        }

    def retrieval_trace(self, query: str, source: str | None = None) -> dict:
        t0 = time.perf_counter()
        scored = self.index.score_units(query)
        if source is not None:
            scored = [item for item in scored if item[0] == source]
        picked = [
            {"unit_id": unit_id, "score": score}
            for unit_id, score in scored[: self.config.top_k]
        ]
        return {
            "query": query, "source": source,
            "units_scored": len(scored), "picked": picked,
            "latency_ms": (time.perf_counter() - t0) * 1000,
        }

    def search(self, query: str, k: int, source: str | None = None) -> list[Hit]:
        trace = self.retrieval_trace(query, source)
        ordered: list[str] = []
        scores: dict[str, float] = {}
        for picked in trace["picked"]:
            unit = self._units[picked["unit_id"]]
            for child_id in unit.member_child_ids:
                scores.setdefault(child_id, picked["score"])
                ordered.append(child_id)
        if source is not None:
            ordered = [
                c for c in ordered
                if self.hierarchy.node(c).source == source
            ]
        ordered = ordered[:k]
        return [
            Hit(node_id=child_id, score=scores[child_id], rank=rank,
                dense_score=scores[child_id])
            for rank, child_id in enumerate(ordered, start=1)
        ]

    def paragraphs_for_units(self, unit_ids: Sequence[str]) -> dict[str, str]:
        paragraphs: dict[str, str] = {}
        for unit_id in unit_ids:
            unit = self._units.get(unit_id)
            if unit is None:
                continue
            for _, _, paragraph_id, paragraph_text in unit.paragraphs:
                paragraphs.setdefault(paragraph_id, paragraph_text)
        return paragraphs


def build_reader_prompt(query: str, units: Sequence[LongRagUnit]) -> str:
    blocks = "\n\n".join(
        f"[unit {i + 1} | {unit.source}]\n{unit.text}"
        for i, unit in enumerate(units)
    )
    return (
        "Answer the question using only the long-context units below. "
        "Write a concise free-form answer; put [unit N] markers after each "
        "factual statement.\n\nQuestion: " + query + "\n\nUnits:\n" + blocks
    )


def answer_to_claims(
    answer: str, unit_ids: Sequence[str], unit_texts: Sequence[str]
) -> list[tuple[str, int]]:
    """Map each answer sentence to its best supporting unit (token-F1).

    Adapter (not part of official LongRAG): lets free-form reader output flow
    through the repo's claim-level verification harness. Each sentence cites
    exactly the unit with the highest token overlap.
    """
    import re
    from collections import Counter

    def token_f1(first: str, second: str) -> float:
        first_tokens, second_tokens = first.lower().split(), second.lower().split()
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

    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", answer.strip())
        if part.strip()
    ]
    claims: list[tuple[str, int]] = []
    for sentence in sentences:
        scores = [token_f1(sentence, text) for text in unit_texts]
        best = max(range(len(unit_ids)), key=lambda i: scores[i])
        claims.append((sentence, best))
    return claims


class GeminiLongReader:
    """Long-context reader via the Gemini API (official LongRAG reader)."""

    def __init__(self, model: str = "gemini-1.5-pro", api_key: str | None = None,
                 max_tokens: int = 2048):
        self.model = model
        self.max_tokens = max_tokens
        try:
            from google import genai
        except Exception as exc:
            raise RuntimeError(
                "LongRAG faithful reader requires the 'google-genai' package."
            ) from exc
        import os

        key = api_key or os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError(
                "LongRAG faithful reader requires a Gemini API key "
                "(config reader_api_key or GEMINI_API_KEY)."
            )
        self.client = genai.Client(api_key=key)

    def generate_answer(self, query: str, units: Sequence[LongRagUnit]) -> str:
        response = self.client.models.generate_content(
            model=self.model,
            contents=build_reader_prompt(query, list(units)),
        )
        return (getattr(response, "text", "") or "").strip()


class LocalFreeReader:
    """Offline long-context reader from a local instruction-tuned LLM.

    Used when no long-context LLM API quota is available. Free-form answer
    from the concatenated long units (official LongRAG reader input); the
    substitution is recorded in run metadata (``reader`` field).
    Deterministic (greedy decoding).
    """

    def __init__(self, model: str = "Qwen/Qwen2.5-7B-Instruct",
                 device: str = "cuda", max_new_tokens: int = 512):
        self.model = model
        self.max_new_tokens = max_new_tokens
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except Exception as exc:
            raise RuntimeError(
                "Local reader requires 'transformers' and 'torch'."
            ) from exc
        use_cuda = device == "cuda" and torch.cuda.is_available()
        self.device = "cuda" if use_cuda else "cpu"
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(model)
            self.model_nn = AutoModelForCausalLM.from_pretrained(
                model, dtype=torch.float16 if use_cuda else torch.float32,
                device_map="auto" if use_cuda else None,
            )
            if not use_cuda:
                self.model_nn = self.model_nn.to("cpu")
            self.model_nn.eval()
        except Exception as exc:
            raise RuntimeError(
                f"Could not load reader model '{model}'. Check network "
                "access to huggingface.co or pre-download the weights."
            ) from exc

    def generate_answer(self, query: str, units: Sequence[LongRagUnit]) -> str:
        import torch

        messages = [
            {"role": "system",
             "content": "Answer concisely using only the units below."},
            {"role": "user", "content": build_reader_prompt(query, list(units))},
        ]
        inputs = self.tokenizer.apply_chat_template(
            messages, return_tensors="pt", add_generation_prompt=True,
            return_dict=True,
        )
        input_ids = inputs["input_ids"]
        attention_mask = inputs.get("attention_mask")
        if self.device == "cuda":
            input_ids = input_ids.to("cuda")
            if attention_mask is not None:
                attention_mask = attention_mask.to("cuda")
        with torch.inference_mode():
            output = self.model_nn.generate(
                input_ids, attention_mask=attention_mask,
                max_new_tokens=self.max_new_tokens, do_sample=False,
            )
        return self.tokenizer.decode(
            output[0][input_ids.shape[1]:], skip_special_tokens=True
        ).strip()


class OpenAILongReader:
    """Long-context reader via the OpenAI API (official LongRAG reader)."""

    def __init__(self, model: str = "gpt-4o", api_key: str | None = None,
                 max_tokens: int = 2048):
        self.model = model
        self.max_tokens = max_tokens
        try:
            from openai import OpenAI
        except Exception as exc:
            raise RuntimeError(
                "LongRAG faithful reader requires the 'openai' package."
            ) from exc
        import os

        key = api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError(
                "LongRAG faithful reader requires an OpenAI API key "
                "(config reader_api_key or OPENAI_API_KEY)."
            )
        self.client = OpenAI(api_key=key)

    def generate_answer(self, query: str, units: Sequence[LongRagUnit]) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user",
                       "content": build_reader_prompt(query, list(units))}],
            max_tokens=self.max_tokens,
        )
        return (response.choices[0].message.content or "").strip()
