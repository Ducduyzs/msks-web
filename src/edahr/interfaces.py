from __future__ import annotations

import inspect
from dataclasses import replace
from typing import Protocol, Sequence

from .schemas import ContextBlock, Generation, Hierarchy, Hit, Node


class Retriever(Protocol):
    def search(
        self, query: str, k: int, source: str | None = None
    ) -> list[Hit]: ...


def scoped_search(
    retriever: Retriever,
    hierarchy: Hierarchy,
    query: str,
    k: int,
    source: str | None = None,
) -> list[Hit]:
    """Rank inside ``source`` before applying ``k``.

    Native retrievers receive the source constraint directly. Legacy and test
    retrievers are over-fetched across every child and filtered afterwards;
    this fallback is slower, but unlike filtering a global top-k it remains
    correct for single-document QA.
    """
    if source is None:
        return retriever.search(query, k)

    try:
        parameters = inspect.signature(retriever.search).parameters.values()
        supports_source = any(
            parameter.name == "source"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        supports_source = False

    requested = k if supports_source else max(k, len(hierarchy.child_ids))
    hits = (
        retriever.search(query, requested, source=source)
        if supports_source
        else retriever.search(query, requested)
    )
    filtered = [
        hit for hit in hits
        if hit.node_id in hierarchy.nodes
        and hierarchy.node(hit.node_id).source == source
    ][:k]
    return [replace(hit, rank=rank) for rank, hit in enumerate(filtered, start=1)]


class Reranker(Protocol):
    def score(self, query: str, texts: Sequence[str]) -> list[float]: ...


class Generator(Protocol):
    def generate(self, query: str, context: Sequence[ContextBlock]) -> Generation: ...


class Verifier(Protocol):
    def support_score(self, claim: str, evidence: str) -> float: ...

