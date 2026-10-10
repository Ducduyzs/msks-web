"""FastAPI backend (ARCHITECTURE.md mục 10).

    uvicorn msks.api:app --port 8000

API không chạy model: nó xác thực, chốt phạm vi, ghi run/job/event trong một transaction và trả 202;
worker (`python -m msks.worker`) xử lý phần nặng.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, File, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import db
from .api_deps import (  # noqa: F401 — re-export cho router và test
    UserDep, _uuid, decode_cursor, encode_cursor, idempotent_job, require_owned, require_workspace, source_row,
)
from .auth import CSRF_COOKIE, SESSION_COOKIE, verify_token
from .dto import SOURCE_SELECT, TERMINAL, job_dto, run_dto, run_event_dto, run_summary_dto, run_to_markdown, source_dto, workspace_dto
from .errors import AppError, PermanentError, feature_disabled, not_found
from .ingest import sha256, sniff
from .profiles import choose_run_profile, get_profile
from .settings import EDAHR_COMMIT, code_hash, get_settings
from .storage import BlobStore

log = logging.getLogger("msks.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.open_pool()
    yield
    db.close_pool()


app = FastAPI(title="MSKS API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------ lỗi + trace


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    request.state.trace_id = uuid.uuid4().hex[:16]
    response = await call_next(request)
    response.headers["X-Trace-Id"] = request.state.trace_id
    return response


def _trace(request: Request) -> str | None:
    return getattr(request.state, "trace_id", None)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(exc.body(_trace(request)), status_code=exc.status)


@app.exception_handler(PermanentError)
async def permanent_error_handler(request: Request, exc: PermanentError):
    status = 413 if exc.code == "file_too_large" else 415 if exc.code == "unsupported_media_type" else 422
    return JSONResponse(AppError(status, exc.code, exc.message).body(_trace(request)), status_code=status)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    message = f"Dữ liệu không hợp lệ: {'.'.join(str(p) for p in first.get('loc', []))} — {first.get('msg', '')}"
    return JSONResponse(AppError(422, "invalid_request", message).body(_trace(request)), status_code=422)


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    log.exception("Lỗi chưa xử lý (trace %s)", _trace(request))
    return JSONResponse(AppError(500, "internal_error", "Lỗi máy chủ.", retryable=True).body(_trace(request)), status_code=500)


# ---------------------------------------------------------------- helpers


# ------------------------------------------------------------ health/auth


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/ready")
def ready():
    settings = get_settings()
    components: dict[str, str] = {}
    try:
        with db.tx() as conn:
            conn.execute("select 1")
            stale = conn.execute(
                "select count(*) as n from public.job where state = 'queued' and created_at < now() - interval '2 minutes'"
            ).fetchone()["n"]
        components["postgres"] = "ok"
        components["worker"] = "degraded" if stale else "ok"
    except Exception:
        components["postgres"] = "down"
    components["storage"] = "ok" if BlobStore().healthy() else "down"
    components["llm"] = "ok" if settings.llm_provider != "none" and settings.openai_api_key else "down"
    _, _, calibrated = settings.thresholds
    return {
        "ready": components.get("postgres") == "ok" and components["storage"] == "ok",
        "components": components,
        "llm_provider": f"OpenAI ({settings.llm_model})" if settings.llm_provider == "openai" else None,
        "calibrated": calibrated,
    }


class SessionIn(BaseModel):
    access_token: str = Field(min_length=20)


@app.post("/api/auth/session")
def create_session(body: SessionIn, response: Response):
    """Đổi access token Supabase lấy cookie HttpOnly cùng origin + CSRF token."""
    user = verify_token(body.access_token)
    settings = get_settings()
    max_age = max(60, user.expires_at - int(time.time()))
    response.set_cookie(SESSION_COOKIE, body.access_token, max_age=max_age, httponly=True,
                        secure=settings.cookie_secure, samesite="lax", path="/api")
    response.set_cookie(CSRF_COOKIE, secrets.token_urlsafe(24), max_age=max_age, httponly=False,
                        secure=settings.cookie_secure, samesite="lax", path="/")
    return {"user": {"id": user.id, "email": user.email}, "expires_at": user.expires_at}


@app.delete("/api/auth/session", status_code=204)
def delete_session(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/api")
    response.delete_cookie(CSRF_COOKIE, path="/")


@app.get("/api/auth/me")
def me(user: UserDep):
    return {"id": user.id, "email": user.email}


# -------------------------------------------------------------- workspace

WORKSPACE_SELECT = """
select w.*,
  (select count(*) from public.source s where s.workspace_id = w.id and s.status <> 'deleted') as source_count,
  (select count(*) from public.run r where r.workspace_id = w.id and r.parent_run_id is null) as run_count
from public.workspace w
"""


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@app.get("/api/workspaces")
def list_workspaces(user: UserDep):
    with db.tx() as conn:
        rows = conn.execute(WORKSPACE_SELECT + " where w.owner_id = %s order by w.created_at desc", (user.id,)).fetchall()
    return [workspace_dto(r) for r in rows]


@app.post("/api/workspaces", status_code=201)
def create_workspace(body: WorkspaceIn, user: UserDep):
    name = body.name.strip()
    if not name:
        raise AppError(422, "invalid_name", "Tên workspace không được để trống.")
    with db.tx() as conn:
        row = conn.execute(
            "insert into public.workspace (owner_id, name) values (%s, %s) returning *", (user.id, name)
        ).fetchone()
    return workspace_dto(row)


@app.get("/api/workspaces/{workspace_id}")
def get_workspace(workspace_id: str, user: UserDep):
    with db.tx() as conn:
        require_workspace(conn, workspace_id, user)
        row = conn.execute(WORKSPACE_SELECT + " where w.id = %s", (workspace_id,)).fetchone()
    return workspace_dto(row)


# ----------------------------------------------------------------- nguồn


@app.get("/api/workspaces/{workspace_id}/sources")
def list_sources(workspace_id: str, user: UserDep, cursor: str | None = None, limit: int = Query(50, ge=1, le=100)):
    after = decode_cursor(cursor)
    with db.tx() as conn:
        require_workspace(conn, workspace_id, user)
        rows = conn.execute(
            SOURCE_SELECT + """ where s.workspace_id = %s and s.status <> 'deleted'
              and (%s::timestamptz is null or (s.created_at, s.id) > (%s::timestamptz, %s::uuid))
              order by s.created_at, s.id limit %s""",
            (workspace_id, after and after[0], after and after[0], after and after[1], limit + 1),
        ).fetchall()
    items = rows[:limit]
    next_cursor = encode_cursor(items[-1]["created_at"], items[-1]["id"]) if len(rows) > limit else None
    return {"items": [source_dto(r) for r in items], "next_cursor": next_cursor}


@app.post("/api/workspaces/{workspace_id}/sources", status_code=202)
async def upload_source(
    workspace_id: str,
    user: UserDep,
    file: UploadFile | None = File(None),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    settings = get_settings()
    if file is None:
        # URL/DOI là JSON và thuộc giai đoạn G3.
        raise feature_disabled("Thêm nguồn từ URL/DOI") if not settings.enable_remote_sources else AppError(
            501, "not_implemented", "Connector URL/DOI chưa được cài đặt.")
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise AppError(413, "file_too_large", "File vượt giới hạn 50 MB.")
    filename = (file.filename or "document").replace("\\", "/").split("/")[-1][:200]
    kind, mime = sniff(filename, data)
    digest = sha256(data)
    request_hash = sha256(f"{filename}|{digest}")
    with db.tx() as conn:
        require_workspace(conn, workspace_id, user)
        existing = idempotent_job(conn, workspace_id, "ingest", idempotency_key, request_hash)
        if existing:
            return source_dto(source_row(conn, existing["target_id"]))
    source_id, revision_id = str(uuid.uuid4()), str(uuid.uuid4())
    object_key = f"{workspace_id}/{source_id}/{revision_id}"  # tên object do server tạo (mục 15)
    title = filename.rsplit(".", 1)[0] or filename
    with db.tx() as conn:
        require_workspace(conn, workspace_id, user)
        # The earlier transaction was released before this one. Recheck under
        # the same-key advisory lock before uploading or inserting anything.
        existing = idempotent_job(conn, workspace_id, "ingest", idempotency_key, request_hash)
        if existing:
            return source_dto(source_row(conn, existing["target_id"]))
        await asyncio.to_thread(BlobStore().put, object_key, data, mime)
        alias = conn.execute("select public.next_source_alias(%s) as a", (workspace_id,)).fetchone()["a"]
        conn.execute(
            """insert into public.source (id, workspace_id, alias, kind, source_type, title, origin_group_id)
               values (%s, %s, %s, %s, 'user_note', %s, %s)""",
            (source_id, workspace_id, alias, kind, title, f"g-{source_id[:8]}"),
        )
        conn.execute(
            """insert into public.document_revision (id, workspace_id, source_id, revision_no, mime, object_key, sha256, byte_size,
               license_metadata_json) values (%s, %s, %s, 1, %s, %s, %s, %s, %s)""",
            (revision_id, workspace_id, source_id, mime, object_key, digest, len(data),
             db.jsonb({"uploaded_by": user.id, "filename": filename})),
        )
        conn.execute(
            """insert into public.job (workspace_id, target_type, target_id, kind, idempotency_key, request_hash, max_attempts)
               values (%s, 'source', %s, 'ingest', %s, %s, %s)""",
            (workspace_id, source_id, idempotency_key, request_hash, settings.max_job_attempts),
        )
        return source_dto(source_row(conn, source_id))


@app.delete("/api/sources/{source_id}", status_code=202)
def delete_source(source_id: str, user: UserDep):
    with db.tx() as conn:
        source = require_owned(conn, "source", source_id, user, "Nguồn")
        if source["deleted_at"]:
            job = conn.execute(
                "select id from public.job where target_id = %s and kind = 'delete' order by created_at desc limit 1", (source["id"],)
            ).fetchone()
            return {"job_id": str(job["id"]) if job else None}
        # Tombstone ngay để chặn truy xuất/run mới; GC chạy nền (mục 6.5).
        conn.execute("update public.source set status = 'deleting', deleted_at = now() where id = %s", (source["id"],))
        job = conn.execute(
            """insert into public.job (workspace_id, target_type, target_id, kind) values (%s, 'source', %s, 'delete')
               returning id""",
            (source["workspace_id"], source["id"]),
        ).fetchone()
    return {"job_id": str(job["id"])}


@app.get("/api/sources/{source_id}/content")
def source_content(source_id: str, user: UserDep, revision_id: str):
    with db.tx() as conn:
        source = require_owned(conn, "source", source_id, user, "Nguồn")
        if source["deleted_at"]:
            raise AppError(410, "source_deleted", "Nguồn đã bị xóa; nội dung không còn được hiển thị.")
        row = conn.execute(
            """select p.id, p.canonical_text, p.pages_json from public.parse_revision p
               join public.document_revision d on d.id = p.document_revision_id
               where p.id = %s and d.source_id = %s and p.canonical_text is not null""",
            (_uuid(revision_id, "Phiên bản"), source["id"]),
        ).fetchone()
    if not row:
        raise not_found("Phiên bản parse")
    return {"source_id": str(source["id"]), "parse_revision_id": str(row["id"]), "text": row["canonical_text"], "pages": row["pages_json"]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user: UserDep):
    with db.tx() as conn:
        return job_dto(require_owned(conn, "job", job_id, user, "Job"))


# --------------------------------------------------------------------- QA


class QaIn(BaseModel):
    question: str = Field(min_length=3, max_length=4000)
    source_ids: list[str] = Field(default_factory=list, max_length=500)
    budget: Literal[512, 1024, 2048] = 2048
    mode: Literal["standard"] = "standard"
    rerun_of: str | None = None
    profile: Literal["product", "lecture_vi_v1"] | None = None


QUERYABLE = ("ready", "ready_limited")


def scope_snapshot(conn, workspace_id: str, source_ids: list[str], requested_profile: str | None = None) -> dict:
    """Chốt document/parse revision, index generation, nhóm nguồn và profile lúc bắt đầu run (mục 5.4).

    Cả phạm vi phải có generation của **cùng một profile** — không trộn embedding/ngưỡng giữa profile
    (LECTURE mục 7). Nguồn thiếu generation của profile được chọn → 422 kèm hướng dẫn reindex.
    """
    wanted = [_uuid(s, "Nguồn") for s in source_ids]
    candidates = conn.execute(
        """select id, alias, kind from public.source
           where workspace_id = %s and status = any(%s) and deleted_at is null
             and (cardinality(%s::uuid[]) = 0 or id = any(%s::uuid[]))
           order by alias""",
        (workspace_id, list(QUERYABLE), wanted, wanted),
    ).fetchall()
    found = {str(r["id"]) for r in candidates}
    missing = [s for s in wanted if s not in found]
    if missing:
        raise AppError(422, "source_not_ready", f"{len(missing)} nguồn được chọn không tồn tại hoặc chưa sẵn sàng.")
    if not candidates:
        raise AppError(422, "empty_scope", "Phạm vi không có nguồn nào sẵn sàng.")
    profile = choose_run_profile([r["kind"] for r in candidates], requested_profile)
    rows = conn.execute(
        """
        select distinct on (s.id) s.id as source_id, s.alias, s.kind, s.origin_group_id, s.grouping_version,
               d.id as document_revision_id, p.id as parse_revision_id, g.id as index_generation_id
        from public.source s
        join public.document_revision d on d.source_id = s.id
        join public.parse_revision p on p.document_revision_id = d.id and p.status = 'succeeded'
        join public.index_generation g on g.parse_revision_id = p.id and g.state = 'active' and g.model_profile = %s
        where s.id = any(%s::uuid[])
        order by s.id, g.published_at desc
        """,
        (profile, [str(r["id"]) for r in candidates]),
    ).fetchall()
    indexed = {str(r["source_id"]) for r in rows}
    lacking = [r["alias"] for r in candidates if str(r["id"]) not in indexed]
    if lacking:
        raise AppError(
            422, "profile_index_missing",
            f"{', '.join(lacking)} chưa có index cho profile {profile}. Chọn phạm vi khác hoặc gọi "
            f"POST /api/sources/{{id}}/reindex với profile \"{profile}\".",
        )
    return {
        "profile": profile,
        "requested_source_ids": wanted,
        "sources": sorted(({k: str(v) if isinstance(v, uuid.UUID) else v for k, v in r.items()} for r in rows),
                          key=lambda r: r["alias"]),
    }


@app.post("/api/workspaces/{workspace_id}/qa", status_code=202)
def ask(workspace_id: str, body: QaIn, user: UserDep,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    settings = get_settings()
    request_hash = sha256(json.dumps(body.model_dump(), sort_keys=True))
    with db.tx() as conn:
        require_workspace(conn, workspace_id, user)
        existing = idempotent_job(conn, workspace_id, "run", idempotency_key, request_hash)
        if existing:
            return _accepted(str(existing["target_id"]))
        snapshot = scope_snapshot(conn, workspace_id, body.source_ids, body.profile)
        profile = get_profile(snapshot["profile"])
        rerun_of = None
        if body.rerun_of:
            previous = require_owned(conn, "run", body.rerun_of, user, "Lượt chạy")
            if str(previous["workspace_id"]) != str(workspace_id):
                raise AppError(422, "rerun_workspace_mismatch", "Lượt chạy gốc phải thuộc cùng workspace.")
            rerun_of = str(previous["id"])
        models = {
            "embedding": profile.embedding_model, "reranker": profile.reranker_model, "sbert": profile.sbert_model,
            "nli": profile.nli_model, "llm": f"{settings.llm_provider}:{settings.llm_model}",
            "profile_digest": profile.digest(),
            "edahr_commit": EDAHR_COMMIT, "verifier_revision": profile.nli_model,
            "calibration_artifact": profile.calibration_artifact,
        }
        run = conn.execute(
            """insert into public.run (workspace_id, rerun_of, created_by, mode, profile, query, config_hash, code_hash,
               model_ids_json, scope_snapshot_json, grouping_snapshot_json, budget_json)
               values (%s, %s, %s, 'qa', %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
            (workspace_id, rerun_of, user.id, profile.name, body.question.strip(), settings.config_hash(), code_hash(),
             db.jsonb(models), db.jsonb(snapshot),
             db.jsonb({"grouping_version": max(s["grouping_version"] for s in snapshot["sources"]),
                       "groups": {s["source_id"]: s["origin_group_id"] for s in snapshot["sources"]}}),
             db.jsonb({"context_tokens": body.budget})),
        ).fetchone()
        conn.execute(
            """insert into public.job (workspace_id, run_id, target_type, target_id, kind, idempotency_key, request_hash, max_attempts)
               values (%s, %s, 'run', %s, 'run', %s, %s, %s)""",
            (workspace_id, run["id"], run["id"], idempotency_key, request_hash, settings.max_job_attempts),
        )
        db.append_run_event(conn, str(run["id"]), "stage", {"stage": "queued", "label": "Đã xếp hàng"})
    return _accepted(str(run["id"]))


def _accepted(run_id: str) -> dict:
    return {"run_id": run_id, "status_url": f"/api/runs/{run_id}", "stream_url": f"/api/runs/{run_id}/stream"}


@app.post("/api/workspaces/{workspace_id}/synthesis/outline")
def outline(workspace_id: str, user: UserDep):
    raise feature_disabled("Tổng hợp (synthesis)")


@app.post("/api/workspaces/{workspace_id}/synthesis")
def synthesis(workspace_id: str, user: UserDep):
    raise feature_disabled("Tổng hợp (synthesis)")


# ------------------------------------------------------------------- runs


@app.get("/api/workspaces/{workspace_id}/runs")
def list_runs(workspace_id: str, user: UserDep, cursor: str | None = None, limit: int = Query(10, ge=1, le=50)):
    after = decode_cursor(cursor)
    with db.tx() as conn:
        require_workspace(conn, workspace_id, user)
        rows = conn.execute(
            """select r.*, (select count(*) from public.claim c where c.run_id = r.id
                            and c.status in ('supported', 'corroborated', 'contested')) as accepted_count
               from public.run r where r.workspace_id = %s and r.parent_run_id is null
                 and (%s::timestamptz is null or (r.created_at, r.id) < (%s::timestamptz, %s::uuid))
               order by r.created_at desc, r.id desc limit %s""",
            (workspace_id, after and after[0], after and after[0], after and after[1], limit + 1),
        ).fetchall()
    items = rows[:limit]
    next_cursor = encode_cursor(items[-1]["created_at"], items[-1]["id"]) if len(rows) > limit else None
    return {"items": [run_summary_dto(r) for r in items], "next_cursor": next_cursor}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, user: UserDep):
    with db.tx() as conn:
        return run_dto(conn, require_owned(conn, "run", run_id, user, "Lượt chạy"))


@app.post("/api/runs/{run_id}/cancel", status_code=202)
def cancel_run(run_id: str, user: UserDep):
    with db.tx() as conn:
        run = require_owned(conn, "run", run_id, user, "Lượt chạy")
        # Same lock order as the worker: job first, run second.
        conn.execute(
            "select id from public.job where target_id = %s and kind = 'run' order by id for update",
            (run["id"],),
        ).fetchall()
        run = conn.execute("select * from public.run where id = %s for update", (run["id"],)).fetchone()
        if run["status"] in TERMINAL:
            raise AppError(409, "run_not_cancellable", "Lượt chạy đã kết thúc.")
        conn.execute("update public.run set cancel_requested = true where id = %s", (run["id"],))
        # Chưa có worker nhận: hủy ngay, không chờ.
        claimed = conn.execute(
            """update public.job set state = 'cancelled' where target_id = %s and kind = 'run' and state = 'queued'
               returning id""", (run["id"],)
        ).fetchone()
        if claimed:
            conn.execute("update public.run set status = 'cancelled', finished_at = now() where id = %s", (run["id"],))
            db.append_run_event(conn, str(run["id"]), "done", {"status": "cancelled"})
    return {"status": "cancel_requested"}


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str, request: Request, user: UserDep,
                     last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None):
    """SSE đọc run_event đã persist; phát lại sau Last-Event-ID, heartbeat 15 s (mục 4.3)."""
    with db.tx() as conn:
        run = require_owned(conn, "run", run_id, user, "Lượt chạy")
    run_key = str(run["id"])
    start = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

    def fetch(after: int) -> list[dict]:
        with db.tx() as conn:
            require_owned(conn, "run", run_key, user, "Lượt chạy")
            rows = conn.execute(
                "select seq, event_type, payload_json from public.run_event where run_id = %s and seq > %s order by seq limit 200",
                (run_key, after),
            ).fetchall()
            return [run_event_dto(conn, run_key, row) for row in rows]

    async def events():
        last, idle = start, 0.0
        yield "retry: 2000\n\n"
        while True:
            if await request.is_disconnected():
                return
            if time.time() >= user.expires_at:
                return  # reconnect must authenticate again
            rows = await asyncio.to_thread(fetch, last)
            for row in rows:
                last = row["seq"]
                yield f"id: {row['seq']}\ndata: {json.dumps(row, ensure_ascii=False, default=str)}\n\n"
                if row["type"] == "done":
                    return
            if rows:
                idle = 0.0
                continue
            await asyncio.sleep(0.5)
            idle += 0.5
            if idle >= 15:
                idle = 0.0
                yield ": heartbeat\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/export")
def export_run(run_id: str, user: UserDep, format: Literal["md", "json", "docx", "pdf"] = "md"):
    if format in ("docx", "pdf"):
        raise feature_disabled(f"Xuất {format.upper()}")
    with db.tx() as conn:
        run = run_dto(conn, require_owned(conn, "run", run_id, user, "Lượt chạy"))
    if run["status"] not in ("succeeded", "partial"):
        raise AppError(409, "run_not_finished", "Chỉ xuất được lượt chạy đã hoàn tất.")
    name = f"msks-{run['run_id']}.{format}"
    headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    if format == "json":
        return Response(json.dumps(run, ensure_ascii=False, indent=2), media_type="application/json", headers=headers)
    return Response(run_to_markdown(run), media_type="text/markdown; charset=utf-8", headers=headers)


# Bài giảng / media (LECTURE_ARCHITECTURE.md mục 9). Import cuối file để router dùng được helper ở trên.
from .api_media import router as media_router  # noqa: E402

app.include_router(media_router)



