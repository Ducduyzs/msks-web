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

import psycopg

from . import db, jobs
from .errors import PermanentError, TransientError
from .ingest import build_tree, fingerprint_hex, jaccard_estimate, minhash, parse, sha256
from .indexing import LeafRow, check_leaf_quota, embed_and_publish, persist_tree
from .media import pipeline as media_pipeline
from .profiles import LECTURE_KINDS, default_index_profile
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
        if source["status"] in ("ready", "ready_limited", "review_required"):
            return  # publication committed before the previous worker lost its lease
        revision = conn.execute(
            "select * from public.document_revision where source_id = %s order by revision_no desc limit 1", (source["id"],)
        ).fetchone()
    if source["kind"] in LECTURE_KINDS:
        media_pipeline.ingest_media(job, source)
        return
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
    ws = str(source["workspace_id"])
    check_leaf_quota(job, ws, len(leaves))
    parse_revision_id, ids = persist_tree(job, ws, str(revision["id"]), tree)

    # Nhóm nguồn gốc theo fingerprint (mục 6.2): phát hiện bản sao, không chứng minh độc lập.
    assign_origin_group(job, source, tree.canonical_text)

    jobs.set_stage(job, "embedding")
    _source_progress(job, source_id, "embedding", 0.6)
    rows = [LeafRow(ids[n.legacy_node_id], n.text, n.embedding_text) for n in leaves]
    jobs.set_stage(job, "publishing")
    embed_and_publish(job, source_id, ws, parse_revision_id, rows, default_index_profile(source["kind"]))


def assign_origin_group(job: dict, source: dict, canonical_text: str) -> None:
    fingerprint = fingerprint_hex(minhash(canonical_text))
    with jobs.guarded(job) as conn:
        group, reason = source["origin_group_id"], None
        for other in conn.execute(
            """select alias, origin_group_id, origin_fingerprint from public.source
               where workspace_id = %s and id <> %s and deleted_at is null and origin_fingerprint is not null""",
            (source["workspace_id"], source["id"]),
        ):
            estimate = jaccard_estimate(fingerprint, other["origin_fingerprint"])
            if estimate >= 0.8:
                group = other["origin_group_id"]
                reason = f"MinHash Jaccard ước lượng {estimate:.2f} ≥ 0,8 với {other['alias']} (minhash-5shingle v1)"
                break
        conn.execute(
            "update public.source set origin_fingerprint = %s, origin_group_id = %s, grouping_reason = %s where id = %s",
            (fingerprint, group, reason, source["id"]),
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
    BlobStore().delete([k for k in keys if k and not k.endswith("/")])
    media_pipeline.delete_media(source_id)
    with jobs.guarded(job) as conn:
        conn.execute("update public.source set status = 'deleted', progress = 1 where id = %s", (source_id,))


# ----------------------------------------------------------------- reindex


def handle_reindex(job: dict) -> None:
    """payload.action = 'transcript_edit' (revision trích xuất mới từ bản sửa) | 'profile' (index thêm profile)."""
    payload = job["payload_json"] or {}
    with jobs.guarded(job) as conn:
        source = conn.execute("select * from public.source where id = %s", (job["target_id"],)).fetchone()
    if not source or source["deleted_at"]:
        return
    if payload.get("action") == "transcript_edit":
        new_id = media_pipeline.apply_edits(job, source, payload["base_extraction_revision_id"],
                                            payload.get("segments", {}), payload.get("regions", {}), payload.get("user_id"))
        media_pipeline.rebuild(job, source, new_id)
        return
    if payload.get("action") == "profile":
        profile = payload["profile"]
        with jobs.guarded(job) as conn:
            revision = conn.execute(
                """select p.id from public.parse_revision p join public.document_revision d on d.id = p.document_revision_id
                   join public.index_generation g on g.parse_revision_id = p.id and g.state = 'active'
                   where d.source_id = %s order by g.published_at desc limit 1""", (source["id"],)).fetchone()
            if not revision:
                raise PermanentError("no_active_revision", "Nguồn chưa có bản index đang dùng để reindex.")
            leaves = conn.execute(
                "select id, text from public.node where parse_revision_id = %s and level = 'child' order by char_start",
                (revision["id"],)).fetchall()
        # Header truy xuất (tiêu đề tài liệu/section) không được lưu theo node: reindex embed nội dung leaf.
        rows = [LeafRow(str(r["id"]), r["text"], r["text"]) for r in leaves]
        embed_and_publish(job, str(source["id"]), str(source["workspace_id"]), str(revision["id"]), rows, profile,
                          final_status=source["status"])
        return
    raise PermanentError("invalid_reindex", "Yêu cầu reindex không hợp lệ.")


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


HANDLERS = {"ingest": handle_ingest, "delete": handle_delete, "run": handle_run, "reindex": handle_reindex}


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
    if job["kind"] == "ingest":  # reindex lỗi không làm hỏng index đang dùng
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
    last_sweep = 0.0
    db_backoff = 0.0
    while True:
        if time.monotonic() - last_sweep > 60:
            last_sweep = time.monotonic()
            try:
                swept = media_pipeline.sweep()
                if any(swept.values()):
                    log.info("thu gom media: %s", swept)
            except Exception:  # thu gom lỗi không được chặn hàng đợi
                log.warning("thu gom media lỗi: %s", traceback.format_exc())
        try:
            job = jobs.claim_next()
        except psycopg.OperationalError as exc:
            # Mất kết nối DB lúc nhận job: transaction đã rollback, chưa nhận job nào → chờ rồi thử lại, không thoát.
            db_backoff = min(30.0, db_backoff * 2 or 1.0)
            log.warning("mất kết nối DB khi nhận job (%s); thử lại sau %.0f s", str(exc).splitlines()[0], db_backoff)
            time.sleep(db_backoff)
            continue
        db_backoff = 0.0
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
