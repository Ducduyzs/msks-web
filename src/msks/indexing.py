"""Ghi cây node, embed leaf theo profile và publish index generation (ARCHITECTURE.md mục 6.5).

Dùng chung cho tài liệu, bài giảng và reindex. Mọi ghi đi qua `jobs.guarded` (fencing theo attempt).
Generation chỉ chuyển active sau khi đủ điểm, trong một transaction kiểm tra tombstone; khi publish
chỉ thay generation cũ của **cùng profile** — profile khác của cùng nguồn vẫn giữ nguyên.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable

from . import db, jobs
from .errors import TransientError
from .ingest import Tree, sha256
from .ml import get_models, sparse_literal, vector_literal
from .profiles import get_profile
from .settings import get_settings

ORDER = {"document": 0, "section": 1, "parent": 2, "child": 3}


@dataclass
class LeafRow:
    node_id: str
    text: str
    embedding_text: str


def persist_tree(
    job: dict,
    workspace_id: str,
    document_revision_id: str,
    tree: Tree,
    node_extra: dict[str, dict[str, Any]] | None = None,
    after_insert: Callable[[Any, str, dict[str, str]], None] | None = None,
) -> tuple[str, dict[str, str]]:
    """Tạo parse revision (status running) + node + edge. Trả (parse_revision_id, legacy_id → node uuid).

    `node_extra[legacy_id]` có thể chứa modality/start_ms/end_ms (bài giảng). `after_insert(conn, parse_revision_id, ids)`
    chạy trong cùng transaction — dùng để ghi source map.
    """
    settings = get_settings()
    extra = node_extra or {}
    with jobs.guarded(job) as conn:
        parse_revision_id = str(conn.execute(
            """insert into public.parse_revision (workspace_id, document_revision_id, parser, parser_version,
               normalizer_version, chunker_version, config_hash, text_sha256, page_count, leaf_count, status,
               canonical_text, pages_json)
               values (%s, %s, %s, %s, 'edahr-normalize-1', 'edahr-pack_spans-v11', %s, %s, %s, %s, 'running', %s, %s)
               returning id""",
            (workspace_id, document_revision_id, tree.parser, tree.parser_version, settings.config_hash(),
             sha256(tree.canonical_text), tree.page_count, len(tree.leaves), tree.canonical_text,
             db.jsonb(tree.pages) if tree.pages is not None else None),
        ).fetchone()["id"])
        ids = {n.legacy_node_id: str(uuid.uuid4()) for n in tree.nodes}
        with conn.cursor() as cur:
            # Cha trước con để khóa ngoại section/parent hợp lệ.
            cur.executemany(
                """insert into public.node (id, workspace_id, parse_revision_id, legacy_node_id, level, section_id, parent_id,
                   position, text, text_sha256, token_count, page_start, page_end, char_start, char_end, paragraph_ids_json,
                   modality, start_ms, end_ms)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [
                    (ids[n.legacy_node_id], workspace_id, parse_revision_id, n.legacy_node_id, n.level,
                     ids.get(n.section_legacy) if n.section_legacy else None,
                     ids.get(n.parent_legacy) if n.parent_legacy and n.parent_legacy in ids and n.level != "section" else None,
                     n.position, n.text, sha256(n.text), n.token_count, n.page_start, n.page_end, n.char_start, n.char_end,
                     db.jsonb(n.paragraph_ids),
                     extra.get(n.legacy_node_id, {}).get("modality"),
                     extra.get(n.legacy_node_id, {}).get("start_ms"),
                     extra.get(n.legacy_node_id, {}).get("end_ms"))
                    for n in sorted(tree.nodes, key=lambda n: ORDER[n.level])
                ],
            )
            cur.executemany(
                "insert into public.node_edge (workspace_id, parent_node_id, child_node_id, ordinal) values (%s, %s, %s, %s)",
                [(workspace_id, ids[p], ids[c], o) for p, c, o in tree.edges if p in ids and c in ids],
            )
        if after_insert:
            after_insert(conn, parse_revision_id, ids)
    return parse_revision_id, ids


def check_leaf_quota(job: dict, workspace_id: str, new_leaves: int) -> None:
    from .errors import PermanentError

    settings = get_settings()
    with jobs.guarded(job) as conn:
        existing = conn.execute(
            """select count(distinct e.node_id) as n from public.leaf_embedding e
               join public.index_generation g on g.id = e.index_generation_id
               where g.workspace_id = %s and g.state = 'active'""",
            (workspace_id,),
        ).fetchone()["n"]
    if existing + new_leaves > settings.max_leaves_per_workspace:
        raise PermanentError("workspace_leaf_limit", f"Workspace vượt giới hạn {settings.max_leaves_per_workspace} đoạn lá.")


def embed_and_publish(
    job: dict,
    source_id: str,
    workspace_id: str,
    parse_revision_id: str,
    leaves: list[LeafRow],
    profile_name: str,
    *,
    final_status: str = "ready",
    source_updates: dict[str, Any] | None = None,
    on_published: Callable[[Any], None] | None = None,
) -> str | None:
    """Embed theo profile → generation building → kiểm đủ điểm → active. Trả generation id (None nếu nguồn đã xóa)."""
    settings = get_settings()
    profile = get_profile(profile_name)
    models = get_models()
    dense, sparse = models.encode([leaf.embedding_text or leaf.text for leaf in leaves])
    sbert = models.sbert_encode([leaf.text for leaf in leaves], profile.sbert_model)

    with jobs.guarded(job) as conn:
        generation_id = str(conn.execute(
            """insert into public.index_generation (workspace_id, parse_revision_id, embedding_revision, config_hash,
               expected_points, state, model_profile) values (%s, %s, %s, %s, %s, 'building', %s) returning id""",
            (workspace_id, parse_revision_id, profile.embedding_revision, settings.config_hash(), len(leaves), profile.name),
        ).fetchone()["id"])
        with conn.cursor() as cur:
            cur.executemany(
                """insert into public.leaf_embedding (workspace_id, index_generation_id, node_id, dense, sparse, sbert)
                   values (%s, %s, %s, %s::extensions.vector, %s::extensions.sparsevec, %s::extensions.vector)""",
                [(workspace_id, generation_id, leaves[i].node_id, vector_literal(dense[i]), sparse_literal(sparse[i]),
                  vector_literal(sbert[i])) for i in range(len(leaves))],
            )

    with jobs.guarded(job) as conn:
        written = conn.execute(
            "select count(*) as n from public.leaf_embedding where index_generation_id = %s", (generation_id,)
        ).fetchone()["n"]
        if written != len(leaves):
            raise TransientError(f"Index thiếu điểm: {written}/{len(leaves)}")
        current = conn.execute("select deleted_at from public.source where id = %s for update", (source_id,)).fetchone()
        if not current or current["deleted_at"]:
            conn.execute("update public.index_generation set state = 'orphaned' where id = %s", (generation_id,))
            return None
        conn.execute(
            """update public.index_generation g set state = 'retired' from public.parse_revision p
               join public.document_revision d on d.id = p.document_revision_id
               where g.parse_revision_id = p.id and d.source_id = %s and g.state = 'active' and g.model_profile = %s""",
            (source_id, profile.name),
        )
        conn.execute("update public.index_generation set state = 'active', published_at = now() where id = %s", (generation_id,))
        conn.execute("update public.parse_revision set status = 'succeeded' where id = %s", (parse_revision_id,))
        updates = {"status": final_status, "progress": 1, "error_json": None, **(source_updates or {})}
        assignments = ", ".join(f"{column} = %s" for column in updates)
        conn.execute(
            f"update public.source set {assignments} where id = %s",
            (*[db.jsonb(v) if isinstance(v, (dict, list)) else v for v in updates.values()], source_id),
        )
        if on_published:
            on_published(conn)
    return generation_id
