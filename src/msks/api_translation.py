"""API dịch tài liệu (11/10/2026) — bản dịch máy chỉ để đọc; trích dẫn/kiểm chứng luôn trên bản gốc."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from . import db
from .api_deps import UserDep, _uuid, limit, source_row
from .api_media import _require_live_source
from .dto import iso
from .errors import AppError, not_found
from .settings import get_settings
from .translation import LANGUAGES

router = APIRouter()
QUERYABLE = ("ready", "ready_limited", "review_required")


class TranslationIn(BaseModel):
    target_lang: str = Field(min_length=2, max_length=10)


def translation_dto(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "source_id": str(row["source_id"]),
        "parse_revision_id": str(row["parse_revision_id"]),
        "target_lang": row["target_lang"],
        "status": row["status"],
        "progress": float(row["progress"]),
        "segment_count": row["segment_count"],
        "char_count": row["char_count"],
        "flagged_count": row["flagged_count"],
        "model": row["model"],
        "error": row["error_json"],
        "created_at": iso(row["created_at"]),
        "finished_at": iso(row["finished_at"]),
    }


@router.get("/api/translation-languages")
def translation_languages():
    return [{"code": code, "name": name} for code, name in LANGUAGES.items()]


@router.post("/api/sources/{source_id}/translations", status_code=202, dependencies=[limit("translate")])
def request_translation(source_id: str, body: TranslationIn, user: UserDep):
    """Dịch phiên bản hiện tại của nguồn. Idempotent: đã có (đang chạy/xong) thì trả lại; lỗi thì chạy lại phần thiếu."""
    if body.target_lang not in LANGUAGES:
        raise AppError(422, "unsupported_language", "Ngôn ngữ đích chưa được hỗ trợ.")
    settings = get_settings()
    with db.tx() as conn:
        _require_live_source(conn, source_id, user)
        source = source_row(conn, source_id)
        if source["status"] not in QUERYABLE or not source.get("pr_id"):
            raise AppError(409, "source_not_ready", "Nguồn chưa xử lý xong nên chưa dịch được.")
        revision = source["pr_id"]
        stats = conn.execute(
            """select count(*) as n, coalesce(sum(char_length(text)), 0) as chars from public.node
               where parse_revision_id = %s and level = 'child'""", (revision,)).fetchone()
        if not stats["n"]:
            raise AppError(422, "nothing_to_translate", "Nguồn không có đoạn văn bản nào để dịch.")
        if stats["chars"] > settings.max_translation_chars:
            raise AppError(413, "translation_too_large",
                           f"Nguồn dài {stats['chars']:,} ký tự, vượt giới hạn dịch {settings.max_translation_chars:,} ký tự.")
        conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"translate:{revision}:{body.target_lang}",))
        row = conn.execute("select * from public.source_translation where parse_revision_id = %s and target_lang = %s",
                           (revision, body.target_lang)).fetchone()
        if row and row["status"] != "failed":
            return translation_dto(row)
        if row:
            row = conn.execute(
                """update public.source_translation set status = 'queued', error_json = null, finished_at = null,
                   requested_by = %s where id = %s returning *""", (user.id, row["id"])).fetchone()
        else:
            row = conn.execute(
                """insert into public.source_translation (workspace_id, source_id, parse_revision_id, target_lang,
                   segment_count, char_count, requested_by) values (%s, %s, %s, %s, %s, %s, %s) returning *""",
                (source["workspace_id"], source["id"], revision, body.target_lang, stats["n"], stats["chars"], user.id),
            ).fetchone()
        conn.execute(
            """insert into public.job (workspace_id, target_type, target_id, kind, payload_json, max_attempts)
               values (%s, 'source', %s, 'translate', %s, %s)""",
            (source["workspace_id"], source["id"], db.jsonb({"translation_id": str(row["id"])}), settings.max_job_attempts),
        )
    return translation_dto(row)


@router.get("/api/sources/{source_id}/translations")
def list_translations(source_id: str, user: UserDep, revision_id: str | None = None):
    with db.tx() as conn:
        _require_live_source(conn, source_id, user)
        revision = _uuid(revision_id, "Phiên bản") if revision_id else source_row(conn, source_id).get("pr_id")
        rows = conn.execute(
            "select * from public.source_translation where source_id = %s and parse_revision_id = %s order by target_lang",
            (source_id, revision)).fetchall() if revision else []
    return {"parse_revision_id": str(revision) if revision else None, "items": [translation_dto(r) for r in rows]}


@router.get("/api/sources/{source_id}/translations/{target_lang}")
def get_translation(source_id: str, target_lang: str, user: UserDep, revision_id: str):
    """Gốc + dịch theo đoạn lá (kể cả khi đang dịch dở). Cờ `numbers_changed` = số liệu khác bản gốc."""
    with db.tx() as conn:
        _require_live_source(conn, source_id, user)
        row = conn.execute(
            "select * from public.source_translation where source_id = %s and parse_revision_id = %s and target_lang = %s",
            (source_id, _uuid(revision_id, "Phiên bản"), target_lang)).fetchone()
        if not row:
            raise not_found("Bản dịch")
        segments = conn.execute(
            """select s.node_id, s.ordinal, s.text, s.flags, n.text as original, n.char_start, n.char_end, n.page_start,
                      n.page_end, n.modality, n.start_ms, n.end_ms
               from public.translation_segment s join public.node n on n.id = s.node_id
               where s.translation_id = %s order by s.ordinal""", (row["id"],)).fetchall()
    return {
        **translation_dto(row),
        "segments": [{
            "node_id": str(s["node_id"]), "ordinal": s["ordinal"], "original": s["original"], "text": s["text"],
            "flags": s["flags"] or [], "char_start": s["char_start"], "char_end": s["char_end"],
            "page_start": s["page_start"], "page_end": s["page_end"], "modality": s["modality"] or "text",
            "start_ms": s["start_ms"], "end_ms": s["end_ms"],
        } for s in segments],
    }
