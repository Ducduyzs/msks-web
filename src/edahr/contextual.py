"""Contextual chunk representations for retrieval (direction H6).

Leaves are embedded without their surroundings, so a chunk such as "results
improve by 3%" loses which paper, method or dataset it is about. Two ways to
put that context back, applied to *retrieval representations only*
(``Node.embedding_text``); generation, verification and citations keep the
raw leaf text, so evidence and attribution are unchanged:

* ``title`` — deterministic: paper title + section heading (the
  decontextualization PeerQA found to help consistently). Free.
* ``llm``  — Anthropic's Contextual Retrieval: an LLM writes 50-100 tokens
  situating the chunk in its whole paper; the context is prepended. Generated
  once by ``scripts/contextualize_chunks.py`` and cached, so every system and
  rerun embeds identical text.

Child ids hash the raw chunk, not ``embedding_text``, so ids, gold labels and
manifests are unaffected by the mode.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from .schemas import Hierarchy, Level

MODES = ("none", "title", "llm")

# Prompt from Anthropic, "Introducing Contextual Retrieval" (2024).
CONTEXT_PROMPT = """<document>
{document}
</document>
Here is the chunk we want to situate within the whole document
<chunk>
{chunk}
</chunk>
Please give a short succinct context to situate this chunk within the overall document for the purposes of improving search retrieval of the chunk. Answer only with the succinct context and nothing else."""


def document_text(hierarchy: Hierarchy, source: str) -> str:
    sections = [
        node for node in hierarchy.nodes.values()
        if node.level == Level.SECTION and node.source == source
    ]
    return "\n\n".join(f"{node.section_title}\n{node.text}" for node in sections)


def context_key(model: str, document: str, chunk: str) -> str:
    digest = hashlib.sha256()
    for part in (model, document, chunk):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def load_contexts(path: str | Path) -> dict[str, dict]:
    """child_id -> cache record ({"context", "key", "model", ...})."""
    records: dict[str, dict] = {}
    file = Path(path)
    if file.is_file():
        for line in file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                records[record["child_id"]] = record
    return records


def _title(hierarchy: Hierarchy, document_id: str) -> str:
    document = hierarchy.nodes.get(document_id)
    title = (document.metadata or {}).get("title") if document else None
    return str(title or "").strip()


def apply_chunk_context(
    hierarchy: Hierarchy,
    mode: str,
    contexts_path: str | Path | None = None,
    model: str | None = None,
) -> Hierarchy:
    """Return a hierarchy whose child ``embedding_text`` carries context."""
    if mode not in MODES:
        raise ValueError(f"chunk_context must be one of {MODES}, got {mode!r}")
    if mode == "none":
        return hierarchy
    records = load_contexts(contexts_path) if mode == "llm" else {}
    documents: dict[str, str] = {}
    nodes = dict(hierarchy.nodes)
    missing: list[str] = []
    for child_id in hierarchy.child_ids:
        node = nodes[child_id]
        if mode == "title":
            title = _title(hierarchy, node.document_id)
            prefix = f"Paper: {title}\n" if title else ""
            text = f"{prefix}Section: {node.section_title}\n{node.text}"
        else:
            record = records.get(child_id)
            if record is not None and model is not None:
                document = documents.setdefault(
                    node.source, document_text(hierarchy, node.source))
                if record.get("key") != context_key(model, document, node.text):
                    record = None  # stale: different model, paper text or chunk
            if record is None:
                missing.append(child_id)
                continue
            text = f"{record['context'].strip()}\n\n{node.text}"
        nodes[child_id] = replace(node, embedding_text=text)
    if missing:
        raise RuntimeError(
            f"{len(missing)} leaves have no valid cached context in {contexts_path}; "
            "run scripts/contextualize_chunks.py first"
        )
    return Hierarchy(nodes=nodes, child_ids=hierarchy.child_ids)
