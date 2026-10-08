"""Chuyển dòng DB sang JSON đúng hợp đồng frontend (web/src/api/types.ts).

Nguồn đã tombstone: giữ tham chiếu nhưng che nội dung evidence/context (mục 6.5).
"""
from __future__ import annotations

from typing import Any

from psycopg import Connection

TERMINAL = ("succeeded", "partial", "failed", "cancelled")

SOURCE_SELECT = """
select s.*, dr.id as dr_id, dr.revision_no, pr.id as pr_id, pr.page_count, pr.leaf_count,
       (select j.id from public.job j where j.target_type = 'source' and j.target_id = s.id and j.kind = 'ingest'
        order by j.created_at desc limit 1) as job_id
from public.source s
left join lateral (select d.* from public.document_revision d where d.source_id = s.id
                   order by d.revision_no desc limit 1) dr on true
left join lateral (select p.* from public.parse_revision p where p.document_revision_id = dr.id and p.status = 'succeeded'
                   order by p.created_at desc limit 1) pr on true
"""


def iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def source_dto(row: dict) -> dict:
    out = {
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "alias": row["alias"],
        "kind": row["kind"],
        "title": row["title"],
        "authors": row["authors_json"] or [],
        "url": row["url"],
        "doi": row["doi"],
        "published_at": iso(row["published_at"]),
        "source_type": row["source_type"],
        "origin_group_id": row["origin_group_id"],
        "independence_status": row["independence_status"],
        "grouping_version": row["grouping_version"],
        "grouping_reason": row["grouping_reason"],
        "status": row["status"],
        "progress": float(row["progress"]),
        "job_id": str(row["job_id"]) if row.get("job_id") else None,
        "error": row["error_json"],
        "deleted_at": iso(row["deleted_at"]),
        "created_at": iso(row["created_at"]),
        "revision": None,
    }
    if row.get("pr_id"):
        out["revision"] = {
            "document_revision_id": str(row["dr_id"]),
            "revision_no": row["revision_no"],
            "parse_revision_id": str(row["pr_id"]),
            "page_count": row["page_count"],
            "leaf_count": row["leaf_count"],
        }
    return {k: v for k, v in out.items() if v is not None or k in ("revision", "error")}


def workspace_dto(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "created_at": iso(row["created_at"]),
        "source_count": int(row.get("source_count") or 0),
        "run_count": int(row.get("run_count") or 0),
    }


def job_dto(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "target_id": str(row["target_id"]),
        "state": row["state"],
        "stage": row["stage"],
        "retry_count": row["retry_count"],
        "error": row["error_json"],
    }


_EVIDENCE_SQL = """
select ce.*, n.parse_revision_id, n.page_start, n.page_end, n.char_start, n.char_end,
       s.id as source_id, s.alias, s.deleted_at
from public.claim_evidence ce
join public.node n on n.id = ce.node_id
join public.parse_revision pr on pr.id = n.parse_revision_id
join public.document_revision dr on dr.id = pr.document_revision_id
join public.source s on s.id = dr.source_id
where ce.claim_id = any(%s)
order by ce.claim_id, case ce.role when 'primary' then 0 when 'corroborating' then 1 else 2 end, ce.id
"""

_CONTEXT_SQL = """
select cb.*, n.parse_revision_id, n.page_start, n.page_end, s.alias, s.deleted_at
from public.context_block cb
join public.node n on n.id = cb.node_id
join public.parse_revision pr on pr.id = n.parse_revision_id
join public.document_revision dr on dr.id = pr.document_revision_id
join public.source s on s.id = dr.source_id
where cb.run_id = %s
order by cb.rank
"""


def evidence_dto(row: dict) -> dict:
    deleted = row["deleted_at"] is not None
    out = {
        "id": str(row["id"]),
        "role": row["role"],
        "source_id": str(row["source_id"]),
        "source_alias": row["alias"],
        "node_id": str(row["node_id"]),
        "parse_revision_id": str(row["parse_revision_id"]),
        "context_block_id": str(row["context_block_id"]) if row["context_block_id"] else None,
        "page_start": row["page_start"],
        "page_end": row["page_end"],
        "char_start": row["char_start"],
        "char_end": row["char_end"],
        "quote_start": row["quote_start"],
        "quote_end": row["quote_end"],
        "evidence_snapshot": "" if deleted else row["evidence_snapshot"],
        "support": float(row["support"]),
        "contradiction": float(row["contradiction"]),
        "verifier_revision": row["verifier_revision"],
        "position": row["position"],
    }
    if deleted:
        out["source_deleted"] = True
    return out


def claims_with_evidence(conn: Connection, claim_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(claim đã publish, claim bị loại) theo thứ tự ordinal."""
    ids = [row["id"] for row in claim_rows if row["status"] not in ("rejected", "pending")]
    evidence: dict[str, list[dict]] = {}
    if ids:
        for row in conn.execute(_EVIDENCE_SQL, (ids,)).fetchall():
            evidence.setdefault(str(row["claim_id"]), []).append(evidence_dto(row))
    published, rejected = [], []
    for row in claim_rows:
        if row["status"] == "rejected":
            rejected.append({
                "id": str(row["id"]),
                "text": row["text"],
                "reason": row["rejected_reason"],
                "section_key": row["section_key"],
                "support": row["support_score"],
            })
        elif row["status"] != "pending":
            published.append(claim_dto(row, evidence.get(str(row["id"]), [])))
    return published, rejected


def claim_dto(row: dict, evidence: list[dict]) -> dict:
    return {
        "id": str(row["id"]),
        "text": row["text"],
        "section_key": row["section_key"],
        "status": row["status"],
        "cross_check_status": row["cross_check_status"],
        "verification_method": row["verification_method"],
        "generator_confidence": float(row["generator_confidence"] or 0.0),
        "evidence": evidence,
    }


def context_dto(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "context_id": row["context_id"],
        "node_id": str(row["node_id"]),
        "source_alias": row["alias"],
        "parse_revision_id": str(row["parse_revision_id"]),
        "page_start": row["page_start"],
        "page_end": row["page_end"],
        "rank": row["rank"],
        "visible_text": "" if row["deleted_at"] is not None else row["visible_text"],
        "token_count": row["token_count"],
        "truncated": row["truncated"],
        "trace": row["trace_json"],
    }


def run_event_dto(conn: Connection, run_id: str, row: dict) -> dict:
    """Rehydrate claims so replay cannot bypass current source tombstones."""
    payload = row["payload_json"]
    if row["event_type"] == "claim_verified":
        claim_id = (payload.get("claim") or {}).get("id")
        claims = conn.execute(
            "select * from public.claim where run_id = %s and id::text = %s",
            (run_id, claim_id),
        ).fetchall()
        published, _ = claims_with_evidence(conn, claims)
        if not published:
            # Never replay a cached evidence payload if its claim is gone.
            return {"seq": row["seq"], "type": "stage", "stage": "verify", "label": "Bằng chứng không còn khả dụng"}
        payload = {"claim": published[0]}
    return {**payload, "seq": row["seq"], "type": row["event_type"]}


def run_dto(conn: Connection, run: dict) -> dict:
    claim_rows = conn.execute(
        "select * from public.claim where run_id = %s order by ordinal", (run["id"],)
    ).fetchall()
    published, rejected = claims_with_evidence(conn, claim_rows)
    context = [context_dto(r) for r in conn.execute(_CONTEXT_SQL, (run["id"],)).fetchall()]
    sections = [
        {"key": r["key"], "title": r["title"], "status": r["status"], "paragraphs": r["paragraphs_json"]}
        for r in conn.execute("select * from public.report_section where run_id = %s order by ordinal", (run["id"],))
    ]
    snapshot = run["scope_snapshot_json"] or {}
    models = run["model_ids_json"] or {}
    provenance = {
        "profile": run["profile"],
        "config_hash": run["config_hash"],
        "code_hash": run["code_hash"],
        "edahr_commit": models.get("edahr_commit"),
        "verifier_revision": models.get("verifier_revision", ""),
        "calibration_artifact": models.get("calibration_artifact"),
        "grouping_version": int((run["grouping_snapshot_json"] or {}).get("grouping_version", 1)),
        "models": {k: v for k, v in models.items() if k not in ("edahr_commit", "verifier_revision", "calibration_artifact")},
        "source_snapshot": snapshot.get("sources", []),
    }
    out = {
        "run_id": str(run["id"]),
        "workspace_id": str(run["workspace_id"]),
        "parent_run_id": str(run["parent_run_id"]) if run["parent_run_id"] else None,
        "rerun_of": str(run["rerun_of"]) if run["rerun_of"] else None,
        "mode": run["mode"],
        "profile": run["profile"],
        "query": run["query"],
        "query_type": run["query_type"],
        "budget": run["budget_json"],
        "source_ids": snapshot.get("requested_source_ids", []),
        "status": run["status"],
        "answer_status": run["answer_status"],
        "cancel_requested": run["cancel_requested"],
        "error": run["error_json"],
        "claims": published,
        "rejected_claims": rejected if run["status"] in TERMINAL else [],
        "context": context if run["status"] in TERMINAL else [],
        "outline": run["outline_json"],
        "sections": sections if run["mode"] == "synthesis" else None,
        "metrics": run["metrics_json"] if run["status"] in TERMINAL else None,
        "provenance": provenance,
        "created_at": iso(run["created_at"]),
        "finished_at": iso(run["finished_at"]),
    }
    return {k: v for k, v in out.items() if v is not None or k in ("claims", "rejected_claims", "context")}


def run_summary_dto(row: dict) -> dict:
    return {
        "run_id": str(row["id"]),
        "mode": row["mode"],
        "query": row["query"],
        "status": row["status"],
        "answer_status": row["answer_status"],
        "created_at": iso(row["created_at"]),
        "budget": row["budget_json"],
        "rerun_of": str(row["rerun_of"]) if row["rerun_of"] else None,
        "accepted_count": int(row["accepted_count"] or 0),
    }


def run_to_markdown(run: dict) -> str:
    """Xuất từ claim/evidence đã persist, không gọi LLM (mục 11)."""
    lines = [f"# {run['query']}", "", f"> Run {run['run_id']} · {run['status']} · {run.get('answer_status') or '—'} · profile {run['profile']}", ""]

    def cite(claim: dict) -> str:
        e = claim["evidence"][0]
        return f"[{e['source_alias']}{', tr. ' + str(e['page_start']) if e['page_start'] else ''}]"

    by_id = {c["id"]: c for c in run["claims"]}
    if run["mode"] == "synthesis" and run.get("sections"):
        for section in run["sections"]:
            lines += [f"## {section['title']}", ""]
            for paragraph in section["paragraphs"]:
                lines += [paragraph["template_text"], ""]
                lines += [f"- {by_id[i]['text']} {cite(by_id[i])}" for i in paragraph["claim_ids"] if i in by_id]
                lines.append("")
    else:
        lines += [f"- {c['text']} {cite(c)} — {c['status']}" for c in run["claims"]]
        lines.append("")
    lines += ["## Bằng chứng", ""]
    for claim in run["claims"]:
        for e in claim["evidence"]:
            body = "_[nguồn đã xóa — nội dung đã ẩn]_" if e.get("source_deleted") else f"\"{e['evidence_snapshot']}\""
            page = f" tr. {e['page_start']}" if e["page_start"] else ""
            lines.append(f"- **{e['source_alias']}**{page} ({e['role']}, revision {e['parse_revision_id']}): {body}")
    return "\n".join(lines)
