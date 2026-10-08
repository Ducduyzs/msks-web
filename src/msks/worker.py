"""Worker: nhận job từ PostgreSQL và xử lý nạp nguồn, xóa nguồn, chạy QA.

    python -m msks.worker            # chạy liên tục
    python -m msks.worker --once     # xử lý tối đa một job rồi thoát (kiểm thử)

Một process sở hữu GPU, xử lý tuần tự (gpu_max_concurrency = 1, mục 12.2).
"""
from __future__ import annotations

import argparse
import logging
import time
import traceback
import uuid

from . import db, jobs
from .errors import PermanentError, TransientError
from .ingest import build_tree, fingerprint_hex, jaccard_estimate, minhash, parse, sha256
from .ml import get_models, sparse_literal, vector_literal
from .qa import Cancelled, RunState, finish_cancelled, finish_failed, run_qa
from .settings import get_settings
from .storage import BlobStore

log = logging.getLogger("msks.worker")


# ------------------------------------------------------------------ ingest


def _source_progress(job: dict, source_id: str, status: str, progress: float, error: dict | None = None) -> None:
    with jobs.guarded(job) as conn:
        conn.execute(
            """update public.source set status = %s, progress = %s, error_json = %s
               where id = %s and deleted_at is null""",
            (status, progress, db.jsonb(error) if error else None, source_id),
        )


def handle_ingest(job: dict) -> None:
    settings = get_settings()
    with jobs.guarded(job) as conn:
        source = conn.execute("select * from public.source where id = %s", (job["target_id"],)).fetchone()
        if not source or source["deleted_at"]:
            return  # nguồn đã bị xóa trong lúc chờ: bỏ qua
        if source["status"] == "ready":
            return  # publication committed before the previous worker lost its lease
        revision = conn.execute(
            "select * from public.document_revision where source_id = %s order by revision_no desc limit 1", (source["id"],)
        ).fetchone()
    source_id = str(source["id"])

    jobs.set_stage(job, "parsing")
    _source_progress(job, source_id, "parsing", 0.15)
    data = BlobStore().get(revision["object_key"])
    if sha256(data) != revision["sha256"]:
        raise PermanentError("checksum_mismatch", "Nội dung file trong kho không khớp checksum lúc tải lên.")
    paginated = source["kind"] == "pdf"
    document, parser, parser_version = parse(
        source["kind"], data, source["title"], str(revision["id"]), source["alias"], settings.max_pages
    )

    jobs.set_stage(job, "chunking")
    _source_progress(job, source_id, "chunking", 0.35)
    tree = build_tree(document, settings, paginated, parser, parser_version)
    leaves = tree.leaves
    with jobs.guarded(job) as conn:
        existing = conn.execute(
            """select count(*) as n from public.leaf_embedding e join public.index_generation g on g.id = e.index_generation_id
               where g.workspace_id = %s and g.state = 'active'""",
            (source["workspace_id"],),
        ).fetchone()["n"]
    if existing + len(leaves) > settings.max_leaves_per_workspace:
        raise PermanentError("workspace_leaf_limit", f"Workspace vượt giới hạn {settings.max_leaves_per_workspace} đoạn lá.")

    ws = source["workspace_id"]
    with jobs.guarded(job) as conn:
        parse_revision_id = conn.execute(
            """insert into public.parse_revision (workspace_id, document_revision_id, parser, parser_version,
               normalizer_version, chunker_version, config_hash, text_sha256, page_count, leaf_count, status,
               canonical_text, pages_json)
               values (%s, %s, %s, %s, 'edahr-normalize-1', 'edahr-pack_spans-v11', %s, %s, %s, %s, 'running', %s, %s)
               returning id""",
            (ws, revision["id"], parser, parser_version, settings.config_hash(), sha256(tree.canonical_text),
             tree.page_count, len(leaves), tree.canonical_text, db.jsonb(tree.pages) if tree.pages is not None else None),
        ).fetchone()["id"]
        ids = {n.legacy_node_id: str(uuid.uuid4()) for n in tree.nodes}
        # Chèn theo thứ tự cha trước con để khóa ngoại section/parent hợp lệ.
        order = {"document": 0, "section": 1, "parent": 2, "child": 3}
        with conn.cursor() as cur:
            cur.executemany(
                """insert into public.node (id, workspace_id, parse_revision_id, legacy_node_id, level, section_id, parent_id,
                   position, text, text_sha256, token_count, page_start, page_end, char_start, char_end, paragraph_ids_json)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [
                    (ids[n.legacy_node_id], ws, parse_revision_id, n.legacy_node_id, n.level,
                     ids.get(n.section_legacy) if n.section_legacy else None,
                     ids.get(n.parent_legacy) if n.parent_legacy and n.parent_legacy in ids and n.level != "section" else None,
                     n.position, n.text, sha256(n.text), n.token_count, n.page_start, n.page_end, n.char_start, n.char_end,
                     db.jsonb(n.paragraph_ids))
                    for n in sorted(tree.nodes, key=lambda n: order[n.level])
                ],
            )
            cur.executemany(
                "insert into public.node_edge (workspace_id, parent_node_id, child_node_id, ordinal) values (%s, %s, %s, %s)",
                [(ws, ids[p], ids[c], o) for p, c, o in tree.edges if p in ids and c in ids],
            )

    # Nhóm nguồn gốc theo fingerprint (mục 6.2): phát hiện bản sao, không chứng minh độc lập.
    fingerprint = fingerprint_hex(minhash(tree.canonical_text))
    with jobs.guarded(job) as conn:
        group, reason = source["origin_group_id"], None
        for other in conn.execute(
            """select alias, origin_group_id, origin_fingerprint from public.source
               where workspace_id = %s and id <> %s and deleted_at is null and origin_fingerprint is not null""",
            (ws, source_id),
        ):
            estimate = jaccard_estimate(fingerprint, other["origin_fingerprint"])
            if estimate >= 0.8:
                group = other["origin_group_id"]
                reason = f"MinHash Jaccard ước lượng {estimate:.2f} ≥ 0,8 với {other['alias']} (minhash-5shingle v1)"
                break
        conn.execute(
            "update public.source set origin_fingerprint = %s, origin_group_id = %s, grouping_reason = %s where id = %s",
            (fingerprint, group, reason, source_id),
        )

    jobs.set_stage(job, "embedding")
    _source_progress(job, source_id, "embedding", 0.6)
    models = get_models()
    leaf_ids = [ids[n.legacy_node_id] for n in leaves]
    dense, sparse = models.encode([n.embedding_text or n.text for n in leaves])
    sbert = models.sbert_encode([n.text for n in leaves])

    jobs.set_stage(job, "publishing")
    _source_progress(job, source_id, "publishing", 0.85)
    with jobs.guarded(job) as conn:
        generation_id = conn.execute(
            """insert into public.index_generation (workspace_id, parse_revision_id, embedding_revision, config_hash,
               expected_points, state) values (%s, %s, %s, %s, %s, 'building') returning id""",
            (ws, parse_revision_id, f"{settings.embedding_model}+{settings.sbert_model}", settings.config_hash(), len(leaves)),
        ).fetchone()["id"]
        with conn.cursor() as cur:
            cur.executemany(
                """insert into public.leaf_embedding (workspace_id, index_generation_id, node_id, dense, sparse, sbert)
                   values (%s, %s, %s, %s::extensions.vector, %s::extensions.sparsevec, %s::extensions.vector)""",
                [(ws, generation_id, leaf_ids[i], vector_literal(dense[i]), sparse_literal(sparse[i]), vector_literal(sbert[i]))
                 for i in range(len(leaves))],
            )
    # Publish: kiểm tra đủ điểm rồi mới chuyển active, trong một transaction (mục 6.5).
    with jobs.guarded(job) as conn:
        written = conn.execute(
            "select count(*) as n from public.leaf_embedding where index_generation_id = %s", (generation_id,)
        ).fetchone()["n"]
        if written != len(leaves):
            raise TransientError(f"Index thiếu điểm: {written}/{len(leaves)}")
        current = conn.execute("select deleted_at from public.source where id = %s for update", (source_id,)).fetchone()
        if not current or current["deleted_at"]:
            conn.execute("update public.index_generation set state = 'orphaned' where id = %s", (generation_id,))
            return
        conn.execute(
            """update public.index_generation g set state = 'retired' from public.parse_revision p
               join public.document_revision d on d.id = p.document_revision_id
               where g.parse_revision_id = p.id and d.source_id = %s and g.state = 'active'""",
            (source_id,),
        )
        conn.execute("update public.index_generation set state = 'active', published_at = now() where id = %s", (generation_id,))
        conn.execute("update public.parse_revision set status = 'succeeded' where id = %s", (parse_revision_id,))
        conn.execute(
            "update public.source set status = 'ready', progress = 1, error_json = null where id = %s", (source_id,)
        )


# ------------------------------------------------------------------ delete


def handle_delete(job: dict) -> None:
    """GC sau tombstone: xóa vector và file gốc, che nội dung đã lưu; run cũ chỉ giữ tham chiếu (mục 6.5)."""
    source_id = str(job["target_id"])
    with jobs.guarded(job) as conn:
        keys = [r["object_key"] for r in conn.execute(
            "select object_key from public.document_revision where source_id = %s", (source_id,)
        )]
        revisions = [r["id"] for r in conn.execute(
            """select p.id from public.parse_revision p join public.document_revision d on d.id = p.document_revision_id
               where d.source_id = %s""", (source_id,)
        )]
        conn.execute(
            """delete from public.leaf_embedding e using public.index_generation g
               where e.index_generation_id = g.id and g.parse_revision_id = any(%s::uuid[])""", (revisions,)
        )
        conn.execute("update public.index_generation set state = 'retired' where parse_revision_id = any(%s::uuid[])", (revisions,))
        conn.execute("update public.node set text = '', text_sha256 = '' where parse_revision_id = any(%s::uuid[])", (revisions,))
        conn.execute(
            "update public.parse_revision set canonical_text = null, status = 'superseded' where id = any(%s::uuid[])", (revisions,)
        )
        conn.execute(
            """update public.claim_evidence ce set evidence_snapshot = '' from public.node n
               where ce.node_id = n.id and n.parse_revision_id = any(%s::uuid[])""", (revisions,)
        )
        conn.execute(
            """update public.context_block cb set visible_text = '' from public.node n
               where cb.node_id = n.id and n.parse_revision_id = any(%s::uuid[])""", (revisions,)
        )
    BlobStore().delete(keys)
    with jobs.guarded(job) as conn:
        conn.execute("update public.source set status = 'deleted', progress = 1 where id = %s", (source_id,))


# --------------------------------------------------------------------- run


def handle_run(job: dict) -> None:
    with jobs.guarded(job) as conn:
        run = conn.execute("select * from public.run where id = %s", (job["target_id"],)).fetchone()
        if run["status"] in ("succeeded", "partial", "failed", "cancelled"):
            return
        conn.execute("update public.run set status = 'running', started_at = coalesce(started_at, now()) where id = %s", (run["id"],))
    state = RunState(run=run, job_id=str(job["id"]), attempt=job["attempt_version"])
    if run["mode"] != "qa":
        raise PermanentError("feature_disabled", "Synthesis chưa bật trong MVP.")
    try:
        run_qa(state)
    except Cancelled:
        finish_cancelled(state)


HANDLERS = {"ingest": handle_ingest, "delete": handle_delete, "run": handle_run}


def process(job: dict) -> None:
    log.info("job %s kind=%s attempt=%s", job["id"], job["kind"], job["attempt_version"])
    try:
        with jobs.Heartbeat(job):
            HANDLERS[job["kind"]](job)
        jobs.complete(job)
    except jobs.LeaseLost:
        log.info("job %s bỏ qua attempt cũ", job["id"])
    except PermanentError as exc:
        _on_failure(job, exc.code, exc.message, retryable=False)
    except TransientError as exc:
        _on_failure(job, "transient_error", str(exc), retryable=True)
    except Exception as exc:  # lỗi chưa phân loại: thử lại hữu hạn lần
        log.error("job %s lỗi:\n%s", job["id"], traceback.format_exc())
        _on_failure(job, "internal_error", f"{type(exc).__name__}: {exc}"[:300], retryable=True)


def _on_failure(job: dict, code: str, message: str, retryable: bool) -> None:
    try:
        will_retry = jobs.fail(job, code, message, retryable)
    except jobs.LeaseLost:
        return  # do not publish failure for another worker's attempt
    if will_retry:
        log.warning("job %s sẽ thử lại: %s", job["id"], message)
        return
    error = {"code": code, "message": message, "retryable": retryable}
    if job["kind"] == "ingest":
        with jobs.guarded(job, states=("failed",)) as conn:
            conn.execute(
                "update public.source set status = 'failed', error_json = %s where id = %s and deleted_at is null",
                (db.jsonb(error), job["target_id"]),
            )
    elif job["kind"] == "run":
        state = RunState(run={"id": job["target_id"]}, job_id=str(job["id"]), attempt=job["attempt_version"])
        finish_failed(state, code, message, retryable)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle", type=float, default=1.0, help="giây chờ khi hàng đợi trống")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.open_pool(min_size=1, max_size=3)
    log.info("worker sẵn sàng (device=%s, llm=%s)", get_settings().device, get_settings().llm_provider)
    while True:
        job = jobs.claim_next()
        if job:
            process(job)
            if args.once:
                return
            continue
        if args.once:
            return
        time.sleep(args.idle)


if __name__ == "__main__":
    main()
