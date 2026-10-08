"""Faithful RAPTOR baseline (B5_raptor_faithful).

Reimplementation of the official RAPTOR algorithm
(Sarthi et al., ICLR 2024; https://github.com/parthsarthi03/raptor, MIT):

- semantic embeddings (default ``sentence-transformers/multi-qa-mpnet-base-cos-v1``,
  the official SBERT option) — never TF-IDF;
- global UMAP + GMM/BIC clustering, then local UMAP + GMM, soft assignment
  with threshold 0.1, recursive reclustering above 3500 tokens;
- abstractive summaries via the official prompt through a configured LLM;
- collapsed-tree cosine retrieval (top_k=10, max_tokens=3500);
- every tree node maps back to member leaf child ids for leaf-level scoring.

Documented adaptations for QASPER (see analysis/official_baseline_sources.md):
A1. One tree per paper (``source``) instead of one corpus tree, because QASPER
    QA and paper-clustered statistics are paper-scoped.
A2. Leaf layer = EDAHR child chunks (nominally 220 tokens), not official
    100-token chunks, to preserve paragraph/leaf provenance mapping.
A3. Points with empty soft assignment fall back to argmax (official code can
    drop them); this guarantees full leaf coverage for attribution scoring.
A4. Default summarizer model is the official ``gpt-3.5-turbo``; any
    substitution is recorded in index metadata.
A5. When a Gaussian mixture fit fails on an ill-conditioned covariance (the
    official code raises and aborts the tree), it is refit with a larger
    ``reg_covar`` (1e-4, then 1e-3). Fits that needed this are counted in
    ``GMM_REGULARIZED_FITS`` and recorded in the tree metadata.
A6. When UMAP (global or local stage) cannot build a neighbour graph, e.g.
    for near-identical units such as repeated captions (the official code
    raises), the affected points are kept as one cluster -- the treatment the
    official code gives sets too small for UMAP. Counted in ``UMAP_FALLBACKS``.

Primary mode never falls back to lexical methods: missing ``umap-learn``,
sentence-transformers weights, or LLM access raises RuntimeError naming the
missing component. The old lexical variant lives in ``baselines.py`` as
``B5_raptor_lightweight``.
"""

from __future__ import annotations

import hashlib
import math
import pickle
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .interfaces import scoped_search
from .schemas import Hierarchy, Hit

OFFICIAL_EMBEDDING_MODEL = "sentence-transformers/multi-qa-mpnet-base-cos-v1"
OFFICIAL_SUMMARY_PROMPT = (
    "Write a summary of the following, including as many key details as possible: {context}:"
)
OFFICIAL_SUMMARIZER_MODEL = "gpt-3.5-turbo"
OFFICIAL_SEED = 224
OFFICIAL_UMAP_DIM = 10
OFFICIAL_THRESHOLD = 0.1
OFFICIAL_MAX_CLUSTERS = 50
OFFICIAL_MAX_LENGTH_IN_CLUSTER = 3500
OFFICIAL_TOP_K = 10
OFFICIAL_RETRIEVAL_MAX_TOKENS = 3500


@dataclass(frozen=True)
class RaptorFaithfulConfig:
    embedding_model: str = OFFICIAL_EMBEDDING_MODEL
    summarizer_provider: str = "openai"  # "openai" | "gemini"
    summarizer_model: str = OFFICIAL_SUMMARIZER_MODEL
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    umap_dim: int = OFFICIAL_UMAP_DIM
    threshold: float = OFFICIAL_THRESHOLD
    max_clusters: int = OFFICIAL_MAX_CLUSTERS
    max_length_in_cluster: int = OFFICIAL_MAX_LENGTH_IN_CLUSTER
    summary_max_tokens: int = 500
    top_k: int = OFFICIAL_TOP_K
    retrieval_max_tokens: int = OFFICIAL_RETRIEVAL_MAX_TOKENS
    collapsed: bool = True
    max_layers: int = 5
    seed: int = OFFICIAL_SEED
    cache_dir: str = "artifacts/baselines/raptor/index"
    # "text" embeds raw leaves (official); "embedding_text" embeds the
    # contextual representation (edahr.contextual) for the H6 comparison.
    leaf_embedding_source: str = "text"
    device: str = "cuda"


@dataclass
class RaptorFaithfulNode:
    node_id: str
    text: str
    member_child_ids: tuple[str, ...]
    sources: frozenset[str]
    layer: int
    embedding: list[float] = field(default_factory=list)


def _stable_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _token_len(text: str, tokenizer=None) -> int:
    if tokenizer is not None:
        try:
            return len(tokenizer.encode(text))
        except Exception:
            pass
    return max(1, len(text.split()))


def _get_tokenizer():
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def _require_umap():
    try:
        import umap  # noqa: F401

        return True
    except Exception as exc:
        raise RuntimeError(
            "RAPTOR faithful requires 'umap-learn' (official clustering). "
            "Install it: pip install umap-learn"
        ) from exc


def _embed_texts(
    texts: Sequence[str],
    model_name: str,
    device: str,
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
) -> list[list[float]]:
    if embed_fn is not None:
        return [list(map(float, row)) for row in embed_fn(list(texts))]
    model = _cached_sbert_model(model_name, device)
    vectors = model.encode(list(texts), normalize_embeddings=True)
    return [list(map(float, row)) for row in vectors]


_SBERT_CACHE: dict[tuple[str, str], object] = {}


def _cached_sbert_model(model_name: str, device: str):
    key = (model_name, device)
    if key not in _SBERT_CACHE:
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exc:
            raise RuntimeError(
                "RAPTOR faithful requires 'sentence-transformers' for semantic "
                f"embeddings (model {model_name}). No TF-IDF fallback exists in "
                "primary mode."
            ) from exc
        try:
            _SBERT_CACHE[key] = SentenceTransformer(model_name, device=device)
        except Exception as exc:
            raise RuntimeError(
                f"Could not load embedding model '{model_name}'. Check network "
                "access to huggingface.co or pre-download the weights."
            ) from exc
    return _SBERT_CACHE[key]


GMM_REGULARIZED_FITS = [0]
UMAP_FALLBACKS = [0]


def _fit_gmm(vectors, n: int, seed: int):
    """Official GaussianMixture fit; adaptation A5 only if it would raise."""
    from sklearn.mixture import GaussianMixture

    try:
        return GaussianMixture(n_components=n, random_state=seed).fit(vectors)
    except ValueError:
        for reg_covar in (1e-4, 1e-3):
            try:
                gm = GaussianMixture(
                    n_components=n, random_state=seed, reg_covar=reg_covar
                ).fit(vectors)
            except ValueError:
                continue
            GMM_REGULARIZED_FITS[0] += 1
            return gm
        raise


def _gmm_bic_labels(vectors, seed: int, max_clusters: int, threshold: float):
    """GMM with BIC model selection + soft assignment (official logic)."""
    import numpy as np

    count = len(vectors)
    upper = min(max_clusters, count)
    best_n, best_bic = 1, float("inf")
    for n in range(1, upper + 1):
        gm = _fit_gmm(vectors, n, seed)
        bic = gm.bic(vectors)
        if bic < best_bic:
            best_bic, best_n = bic, n
    gm = _fit_gmm(vectors, best_n, seed)
    probs = gm.predict_proba(vectors)
    labels: list[list[int]] = []
    for row in probs:
        assigned = [int(i) for i in np.where(row > threshold)[0]]
        if not assigned:
            # Adaptation A3: argmax fallback guarantees leaf coverage.
            assigned = [int(np.argmax(row))]
        labels.append(assigned)
    return labels, best_n


def raptor_cluster_indices(
    vectors: list[list[float]],
    seed: int = OFFICIAL_SEED,
    dim: int = OFFICIAL_UMAP_DIM,
    threshold: float = OFFICIAL_THRESHOLD,
    max_clusters: int = OFFICIAL_MAX_CLUSTERS,
) -> list[list[int]]:
    """Two-stage UMAP+GMM clustering returning cluster membership per point.

    Mirrors ``perform_clustering`` in official ``cluster_utils.py``: global
    UMAP (n_neighbors=sqrt(N-1), cosine) → GMM/BIC → local UMAP (10 neighbors)
    → GMM/BIC. Points may belong to several clusters (soft assignment).
    """
    _require_umap()
    import numpy as np
    import umap

    matrix = np.asarray(vectors, dtype=np.float64)
    count = len(matrix)
    if count <= 1:
        return [[0]]
    random.seed(seed)
    np.random.seed(seed % (2**32))
    n_neighbors = max(2, int(math.sqrt(count - 1)))
    effective_dim = max(1, min(dim, count - 2))
    try:
        global_reduced = umap.UMAP(
            n_neighbors=n_neighbors, n_components=effective_dim, metric="cosine",
            random_state=seed,
        ).fit_transform(matrix)
        global_labels, n_global = _gmm_bic_labels(
            global_reduced, seed, max_clusters, threshold
        )
    except ValueError:
        UMAP_FALLBACKS[0] += 1
        global_labels, n_global = [[0] for _ in range(count)], 1
    membership: list[set[int]] = [set() for _ in range(count)]
    total = 0
    for global_id in range(n_global):
        members = [i for i, labs in enumerate(global_labels) if global_id in labs]
        if not members:
            continue
        if len(members) <= dim + 1:
            for i in members:
                membership[i].add(total)
            total += 1
            continue
        try:
            local_reduced = umap.UMAP(
                n_neighbors=10, n_components=min(dim, len(members) - 2),
                metric="cosine", random_state=seed,
            ).fit_transform(matrix[members])
        except ValueError:
            UMAP_FALLBACKS[0] += 1
            for i in members:
                membership[i].add(total)
            total += 1
            continue
        local_labels, n_local = _gmm_bic_labels(
            local_reduced, seed, max_clusters, threshold
        )
        for local_id in range(n_local):
            for pos, i in enumerate(members):
                if local_id in local_labels[pos]:
                    membership[i].add(total + local_id)
        total += n_local
    return [sorted(cluster) for cluster in membership]


class OpenAISummarizer:
    """Abstractive summarizer with the official RAPTOR prompt."""

    def __init__(self, model: str = OFFICIAL_SUMMARIZER_MODEL,
                 api_key: str | None = None, max_tokens: int = 500):
        self.model = model
        self.max_tokens = max_tokens
        try:
            from openai import OpenAI
        except Exception as exc:
            raise RuntimeError(
                "RAPTOR faithful summarization requires the 'openai' package."
            ) from exc
        import os

        key = api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError(
                "RAPTOR faithful summarization requires an OpenAI API key "
                "(config openai_api_key or OPENAI_API_KEY). No extractive "
                "fallback exists in primary mode."
            )
        self.client = OpenAI(api_key=key)

    def __call__(self, context: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user",
                 "content": OFFICIAL_SUMMARY_PROMPT.format(context=context)},
            ],
            max_tokens=self.max_tokens,
        )
        return (response.choices[0].message.content or "").strip()


class GeminiSummarizer:
    """Abstractive summarizer with the official RAPTOR prompt via Gemini.

    Used when OpenAI quota is unavailable; the substitution is recorded in
    index metadata (``summarizer_model``) and run metadata.
    """

    def __init__(self, model: str = "gemini-3.8-flash",
                 api_key: str | None = None, max_tokens: int = 500,
                 fallback_models: Sequence[str] = ("gemini-3.7-flash",
                                                   "gemini-3.6-flash")):
        self.model = model
        self.fallback_models = list(fallback_models)
        self.max_tokens = max_tokens
        self.call_log: list[dict] = []
        try:
            from google import genai
        except Exception as exc:
            raise RuntimeError(
                "Gemini summarization requires the 'google-genai' package."
            ) from exc
        import os

        key = api_key or os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError(
                "Gemini summarization requires a Gemini API key "
                "(config gemini_api_key or GEMINI_API_KEY)."
            )
        self.client = genai.Client(api_key=key)

    def __call__(self, context: str) -> str:
        import time as _time

        last_error: Exception | None = None
        for model in [self.model, *self.fallback_models]:
            for attempt in (1, 2, 3, 4, 5):
                try:
                    response = self.client.models.generate_content(
                        model=model,
                        contents=OFFICIAL_SUMMARY_PROMPT.format(context=context),
                    )
                    text = (getattr(response, "text", "") or "").strip()
                    if not text:
                        raise RuntimeError("Gemini summarizer returned empty text.")
                    self.call_log.append({"model": model, "ok": True})
                    return text
                except Exception as exc:  # noqa: BLE001 - retry then record
                    last_error = exc
                    self.call_log.append(
                        {"model": model, "ok": False, "error": str(exc)[:200]}
                    )
                    _time.sleep(min(2**attempt, 30))
        raise RuntimeError(
            f"Gemini summarization failed on all models "
            f"{[self.model, *self.fallback_models]}: {last_error}"
        )


def make_summarizer(config: RaptorFaithfulConfig,
                    gemini_api_key: str | None = None):
    """Build the configured abstractive summarizer (no lexical fallback)."""
    if config.summarizer_provider == "gemini":
        return GeminiSummarizer(
            model=config.summarizer_model, api_key=gemini_api_key,
        )
    if config.summarizer_provider == "openai":
        return OpenAISummarizer(
            model=config.summarizer_model, api_key=config.openai_api_key
        )
    if config.summarizer_provider == "local":
        return LocalAbstractiveSummarizer(
            model=config.summarizer_model, device=config.device
        )
    raise ValueError(
        f"Unknown summarizer_provider {config.summarizer_provider!r}; "
        "expected 'openai', 'gemini' or 'local'."
    )


class LocalAbstractiveSummarizer:
    """Offline abstractive summarizer (HuggingFace seq2seq, e.g. BART).

    Used when no LLM API quota is available. Abstractive (not extractive):
    the model generates novel summary text. Model ID is recorded in index
    metadata. Deterministic given fixed weights (greedy/beam search).
    """

    def __init__(self, model: str = "facebook/bart-large-cnn",
                 device: str = "cuda", max_length: int = 150,
                 min_length: int = 40):
        self.model = model
        self.max_length = max_length
        self.min_length = min_length
        try:
            import torch
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except Exception as exc:
            raise RuntimeError(
                "Local summarization requires 'transformers' and 'torch'."
            ) from exc
        use_cuda = device == "cuda" and torch.cuda.is_available()
        self.device = "cuda" if use_cuda else "cpu"
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(model)
            self.model_nn = AutoModelForSeq2SeqLM.from_pretrained(model)
            if use_cuda:
                self.model_nn = self.model_nn.to("cuda")
            self.model_nn.eval()
        except Exception as exc:
            raise RuntimeError(
                f"Could not load summarization model '{model}'. Check network "
                "access to huggingface.co or pre-download the weights."
            ) from exc

    def __call__(self, context: str) -> str:
        import torch

        inputs = self.tokenizer(
            context, return_tensors="pt", truncation=True, max_length=1024
        )
        if self.device == "cuda":
            inputs = {key: value.to("cuda") for key, value in inputs.items()}
        with torch.inference_mode():
            output = self.model_nn.generate(
                **inputs, max_length=self.max_length,
                min_length=self.min_length, num_beams=4,
                early_stopping=True, do_sample=False,
            )
        text = self.tokenizer.decode(output[0], skip_special_tokens=True).strip()
        if not text:
            raise RuntimeError("Local summarizer returned empty text.")
        return text


@dataclass
class RaptorPaperTree:
    source: str
    nodes: list[RaptorFaithfulNode]
    layers: int
    leaf_child_ids: tuple[str, ...]
    embedding_model: str
    summarizer_model: str

    def node_by_id(self, node_id: str) -> RaptorFaithfulNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)


def build_paper_tree(
    hierarchy: Hierarchy,
    source: str,
    config: RaptorFaithfulConfig,
    summarizer: Callable[[str], str],
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    cache_dir: str | None = None,
) -> tuple[RaptorPaperTree, dict]:
    """Build one faithful RAPTOR tree for a single paper (adaptation A1)."""
    import numpy as np

    tokenizer = _get_tokenizer()
    child_ids = [
        child_id for child_id in hierarchy.child_ids
        if hierarchy.node(child_id).source == source
    ]
    if not child_ids:
        raise ValueError(f"No child leaves for source {source!r}")
    cache_path: Path | None = None
    if cache_dir:
        fingerprint = _stable_key(
            "|".join([
                config.embedding_model, config.summarizer_provider,
                config.summarizer_model,
                str(config.umap_dim), str(config.threshold),
                str(config.max_clusters), str(config.max_length_in_cluster),
                str(config.seed),
                "|".join(f"{c}:{_stable_key(hierarchy.node(c).text)}" for c in child_ids),
            ] + (
                # Appended only when non-default so existing caches stay valid.
                [config.leaf_embedding_source] + [
                    _stable_key(hierarchy.node(c).embedding_text) for c in child_ids
                ]
                if config.leaf_embedding_source != "text" else []
            ))
        )
        cache_path = Path(cache_dir) / f"{_stable_key(source)}-{fingerprint}.pkl"
        if cache_path.is_file():
            with cache_path.open("rb") as handle:
                payload = pickle.load(handle)
            tree = RaptorPaperTree(
                source=payload["source"], layers=payload["layers"],
                leaf_child_ids=tuple(payload["leaf_child_ids"]),
                embedding_model=payload["embedding_model"],
                summarizer_model=payload["summarizer_model"],
                nodes=[
                    RaptorFaithfulNode(
                        node_id=n["node_id"], text=n["text"],
                        member_child_ids=tuple(n["member_child_ids"]),
                        sources=frozenset(n["sources"]), layer=n["layer"],
                        embedding=list(n["embedding"]),
                    )
                    for n in payload["nodes"]
                ],
            )
            return tree, {"cache": "hit", "cache_path": str(cache_path)}

    leaf_texts = [hierarchy.node(child_id).text for child_id in child_ids]
    embedded = (
        [hierarchy.node(child_id).embedding_text for child_id in child_ids]
        if config.leaf_embedding_source == "embedding_text" else leaf_texts
    )
    leaf_vectors = _embed_texts(
        embedded, config.embedding_model, config.device, embed_fn
    )
    nodes = [
        RaptorFaithfulNode(
            node_id=f"leaf-{i}", text=text,
            member_child_ids=(child_id,), sources=frozenset({source}),
            layer=0, embedding=vector,
        )
        for i, (child_id, text, vector) in enumerate(zip(child_ids, leaf_texts, leaf_vectors))
    ]
    current: list[RaptorFaithfulNode] = list(nodes)
    layer = 0
    summary_calls = 0
    regularized_before = GMM_REGULARIZED_FITS[0]
    umap_before = UMAP_FALLBACKS[0]
    while len(current) > 1 and layer < config.max_layers:
        vectors = [node.embedding for node in current]
        if len(current) <= 2:
            clusters: dict[int, list[int]] = {0: list(range(len(current)))}
        else:
            membership = raptor_cluster_indices(
                vectors, seed=config.seed + layer, dim=config.umap_dim,
                threshold=config.threshold, max_clusters=config.max_clusters,
            )
            clusters = {}
            for pos, labs in enumerate(membership):
                for lab in labs:
                    clusters.setdefault(lab, []).append(pos)
        next_level: list[RaptorFaithfulNode] = []
        for label in sorted(clusters):
            members = [current[pos] for pos in clusters[label]]
            total_tokens = sum(_token_len(m.text, tokenizer) for m in members)
            if total_tokens > config.max_length_in_cluster and len(members) > 1:
                # Official recursive reclustering inside oversized clusters.
                sub_vectors = [m.embedding for m in members]
                sub_membership = raptor_cluster_indices(
                    sub_vectors, seed=config.seed + layer + 977,
                    dim=config.umap_dim, threshold=config.threshold,
                    max_clusters=config.max_clusters,
                )
                sub_clusters: dict[int, list[int]] = {}
                for pos, labs in enumerate(sub_membership):
                    for lab in labs:
                        sub_clusters.setdefault(lab, []).append(pos)
                for sub_label in sorted(sub_clusters):
                    sub_members = [members[pos] for pos in sub_clusters[sub_label]]
                    next_level.append(
                        _summarize_nodes(
                            sub_members, source, layer + 1, label * 1000 + sub_label,
                            config, summarizer, embed_fn,
                        )
                    )
                    summary_calls += 1
            else:
                next_level.append(
                    _summarize_nodes(
                        members, source, layer + 1, label,
                        config, summarizer, embed_fn,
                    )
                )
                summary_calls += 1
        if len(next_level) >= len(current):
            break
        current.extend(next_level)
        nodes.extend(next_level)
        layer += 1
        if len(next_level) <= 1:
            break

    tree = RaptorPaperTree(
        source=source, nodes=nodes, layers=layer + 1,
        leaf_child_ids=tuple(child_ids),
        embedding_model=config.embedding_model,
        summarizer_model=config.summarizer_model,
    )
    meta = {
        "cache": "miss", "summary_calls": summary_calls, "layers": layer + 1,
        "gmm_regularized_fits": GMM_REGULARIZED_FITS[0] - regularized_before,
        "umap_fallbacks": UMAP_FALLBACKS[0] - umap_before,
    }
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("wb") as handle:
            pickle.dump(
                {
                    "source": tree.source, "layers": tree.layers,
                    "leaf_child_ids": list(tree.leaf_child_ids),
                    "embedding_model": tree.embedding_model,
                    "summarizer_model": tree.summarizer_model,
                    "nodes": [
                        {
                            "node_id": n.node_id, "text": n.text,
                            "member_child_ids": list(n.member_child_ids),
                            "sources": sorted(n.sources), "layer": n.layer,
                            "embedding": list(n.embedding),
                        }
                        for n in tree.nodes
                    ],
                },
                handle,
            )
        meta["cache_path"] = str(cache_path)
    return tree, meta


def _summarize_nodes(members, source, layer, label, config, summarizer, embed_fn):
    import numpy as np

    member_children: list[str] = []
    for member in members:
        member_children.extend(member.member_child_ids)
    context = "\n\n".join(member.text for member in members)
    summary_text = summarizer(context).strip()
    if not summary_text:
        raise RuntimeError(
            "RAPTOR faithful summarizer returned empty text; aborting "
            "instead of falling back to extractive summary."
        )
    vectors = _embed_texts(
        [summary_text], config.embedding_model, config.device, embed_fn
    )
    return RaptorFaithfulNode(
        node_id=f"L{layer}-c{label}", text=summary_text,
        member_child_ids=tuple(member_children), sources=frozenset({source}),
        layer=layer, embedding=list(vectors[0]),
    )


def save_tree(tree: RaptorPaperTree, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as handle:
        pickle.dump(tree, handle)
    return out


def load_tree(path: str | Path) -> RaptorPaperTree:
    with Path(path).open("rb") as handle:
        tree = pickle.load(handle)
    if not isinstance(tree, RaptorPaperTree):
        raise ValueError(f"Not a RaptorPaperTree: {path}")
    return tree


class RaptorFaithfulRetriever:
    """Collapsed-tree cosine retrieval over faithful RAPTOR trees (per paper)."""

    mode = "faithful"

    def __init__(
        self,
        hierarchy: Hierarchy,
        trees: dict[str, RaptorPaperTree],
        config: RaptorFaithfulConfig,
        embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    ):
        self.hierarchy = hierarchy
        self.trees = dict(trees)
        self.config = config
        self._embed_fn = embed_fn

    def index_metadata(self) -> dict:
        return {
            "baseline": "B5_raptor_faithful",
            "embedding_model": self.config.embedding_model,
            "summarizer_provider": self.config.summarizer_provider,
            "summarizer_model": self.config.summarizer_model,
            "umap_dim": self.config.umap_dim,
            "threshold": self.config.threshold,
            "max_clusters": self.config.max_clusters,
            "max_length_in_cluster": self.config.max_length_in_cluster,
            "top_k": self.config.top_k,
            "retrieval_max_tokens": self.config.retrieval_max_tokens,
            "collapsed": self.config.collapsed,
            "seed": self.config.seed,
            "sources": sorted(self.trees),
            "nodes_per_source": {s: len(t.nodes) for s, t in self.trees.items()},
            "layers_per_source": {s: t.layers for s, t in self.trees.items()},
        }

    def _query_vector(self, query: str) -> list[float]:
        return _embed_texts(
            [query], self.config.embedding_model, self.config.device, self._embed_fn
        )[0]

    def _collapsed_ranking(
        self, query: str, source: str | None
    ) -> list[tuple[RaptorFaithfulNode, float]]:
        import numpy as np

        query_vector = np.asarray(self._query_vector(query), dtype=np.float64)
        scored: list[tuple[RaptorFaithfulNode, float]] = []
        for tree_source, tree in self.trees.items():
            if source is not None and tree_source != source:
                continue
            for node in tree.nodes:
                vector = np.asarray(node.embedding, dtype=np.float64)
                denom = np.linalg.norm(query_vector) * np.linalg.norm(vector)
                similarity = (
                    float(query_vector @ vector / denom) if denom > 0 else 0.0
                )
                scored.append((node, similarity))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored

    def retrieval_trace(self, query: str, source: str | None = None) -> dict:
        t0 = time.perf_counter()
        ranked = self._collapsed_ranking(query, source)
        budget, total, picked = self.config.retrieval_max_tokens, 0, []
        for node, score in ranked[: max(self.config.top_k * 4, 20)]:
            tokens = _token_len(node.text, _get_tokenizer())
            if picked and total + tokens > budget:
                continue
            picked.append(
                {
                    "node_id": node.node_id, "layer": node.layer,
                    "score": score, "tokens": tokens,
                    "member_child_ids": list(node.member_child_ids),
                }
            )
            total += tokens
            if len(picked) >= self.config.top_k:
                break
        return {
            "query": query, "source": source,
            "candidates_scored": len(ranked),
            "internal_nodes_scored": sum(1 for n, _ in ranked if n.layer > 0),
            "picked": picked,
            "latency_ms": (time.perf_counter() - t0) * 1000,
        }

    def search(self, query: str, k: int, source: str | None = None) -> list[Hit]:
        trace = self.retrieval_trace(query, source)
        ordered: list[str] = []
        seen: set[str] = set()
        node_scores: dict[str, float] = {}
        for picked in trace["picked"]:
            for child_id in picked["member_child_ids"]:
                node_scores.setdefault(child_id, picked["score"])
                if child_id not in seen:
                    seen.add(child_id)
                    ordered.append(child_id)
        # scoped_search filters by source for legacy retrievers; our Hits are
        # already source-consistent, but route through it for contract parity.
        hits = [
            Hit(node_id=child_id, score=node_scores[child_id], rank=rank)
            for rank, child_id in enumerate(ordered[:k], start=1)
        ]
        if source is not None:
            hits = [
                hit for hit in hits
                if self.hierarchy.node(hit.node_id).source == source
            ][:k]
            hits = [
                Hit(node_id=h.node_id, score=h.score, rank=r)
                for r, h in enumerate(hits, start=1)
            ]
        return hits
