"""API bài giảng / media (LECTURE_ARCHITECTURE.md mục 3, 4, 9).

Upload theo phiên: API không nhận bytes video. Trình duyệt PUT từng phần (≤ media_part_bytes, do giới
hạn object của gói Storage) vào URL ký sẵn trỏ đúng object key do server sinh; `complete` kiểm tra kích
thước thực rồi mới tạo nguồn + job. Mọi đọc media đi qua kiểm tra quyền + tombstone và trả URL hạn ngắn.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, File, Form, Header, UploadFile
from pydantic import BaseModel, Field

from . import db
from .api_deps import UserDep, _uuid, require_owned, require_workspace, source_row
from .dto import iso, job_dto, source_dto
from .errors import AppError, not_found
from .ingest import sha256
from .media.asr import clean_glossary
from .media.captions import parse_captions
from .profiles import PROFILE_NAMES
from .settings import get_settings
from .storage import BlobStore

router = APIRouter()

VIDEO_EXTENSIONS = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm"}
CAPTION_EXTENSIONS = {".srt": "application/x-subrip", ".vtt": "text/vtt"}
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _media() -> BlobStore:
    return BlobStore(get_settings().media_bucket)


def _filename(name: str | None, default: str) -> str:
    return PurePosixPath((name or default).replace("\\", "/")).name[:200] or default


# ------------------------------------------------------------- glossary ASR


class GlossaryIn(BaseModel):
    terms: list[str] = Field(max_length=200)


@router.put("/api/workspaces/{workspace_id}/asr-glossary")
def put_asr_glossary(workspace_id: str, body: GlossaryIn, user: UserDep):
    """Thuật ngữ đưa vào `initial_prompt` của ASR cho các video **tải lên sau đó** trong workspace (bài đã xử lý
    giữ bản chép cũ; extraction ghi `glossary_sha256`). Danh sách rỗng → dùng glossary toàn cục nếu có."""
    try:
        terms = clean_glossary(body.terms)
    except ValueError as exc:
        raise AppError(422, "invalid_glossary", str(exc)) from exc
    with db.tx() as conn:
        require_workspace(conn, workspace_id, user)
        conn.execute(
            """update public.workspace set settings_json = settings_json || jsonb_build_object('asr_glossary', %s::jsonb)
               where id = %s""", (db.jsonb(terms), workspace_id))
    return {"terms": terms, "applies_to": "new_uploads"}


# ------------------------------------------------------------- upload session


class MediaSessionIn(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    size_bytes: int = Field(gt=0)
    mime: str = Field(min_length=3, max_length=100)
    language: Literal["vi", "en", "auto"] = "auto"
    keep_original: bool = False


def _part_key(session: dict, index: int) -> str:
    return f"{session['object_prefix']}part-{index:04d}"


def _part_size(session: dict, index: int) -> int:
    if index < session["part_count"] - 1:
        return session["part_bytes"]
    return session["expected_bytes"] - session["part_bytes"] * (session["part_count"] - 1)


def _uploaded_sizes(session: dict) -> dict[int, int]:
    store = _media()
    response = httpx.post(f"{store.base}/object/list/{store.bucket}", headers=store.headers,
                          json={"prefix": session["object_prefix"].rstrip("/"), "limit": 1000}, timeout=30)
    response.raise_for_status()
    sizes = {}
    for item in response.json():
        match = re.fullmatch(r"part-(\d{4})", item["name"])
        if match and item.get("id"):
            sizes[int(match.group(1))] = int((item.get("metadata") or {}).get("size") or 0)
    return sizes


def session_dto(session: dict, *, with_urls: bool) -> dict:
    uploaded = _uploaded_sizes(session) if session["status"] == "open" else {}
    store = _media()
    parts = []
    for index in range(session["part_count"]):
        size = _part_size(session, index)
        done = uploaded.get(index) == size
        part = {"index": index, "size": size, "uploaded": done if session["status"] == "open" else session["status"] == "completed"}
        if with_urls and session["status"] == "open" and not done and index not in uploaded:
            # URL ký sẵn chỉ cho đúng object key do server sinh; hạn mặc định của Supabase là 2 giờ.
            part["upload_url"] = store.signed_upload_url(_part_key(session, index))
            part["method"] = "PUT"
        elif index in uploaded and not done:
            part["error"] = "size_mismatch"  # phần đã tải sai kích thước: không ghi đè được, phải hủy phiên
        parts.append(part)
    return {
        "id": str(session["id"]),
        "workspace_id": str(session["workspace_id"]),
        "status": session["status"],
        "filename": session["filename"],
        "mime": session["mime"],
        "expected_bytes": session["expected_bytes"],
        "part_bytes": session["part_bytes"],
        "part_count": session["part_count"],
        "language": session["language"],
        "keep_original": session["keep_original"],
        "caption": {"filename": session["caption_filename"]} if session["caption_object_key"] else None,
        "parts": parts,
        "source_id": str(session["source_id"]) if session["source_id"] else None,
        "expires_at": iso(session["expires_at"]),
    }


def _require_session(conn, session_id: str, user) -> dict:
    session = require_owned(conn, "upload_session", session_id, user, "Phiên upload")
    if str(session["owner_id"]) != user.id:
        raise not_found("Phiên upload")
    return session


def _media_usage(conn, workspace_id: str) -> int:
    return int(conn.execute(
        """select coalesce((select sum(byte_size) from public.media_asset where workspace_id = %s and status = 'ready'), 0)
                + coalesce((select sum(expected_bytes) from public.upload_session
                            where workspace_id = %s and status = 'open' and expires_at > now()), 0) as used""",
        (workspace_id, workspace_id),
    ).fetchone()["used"])


@router.post("/api/workspaces/{workspace_id}/media-upload-sessions", status_code=201)
def create_media_session(workspace_id: str, body: MediaSessionIn, user: UserDep,
                         idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    settings = get_settings()
    filename = _filename(body.filename, "video.mp4")
    extension = PurePosixPath(filename).suffix.lower()
    if extension not in VIDEO_EXTENSIONS or not body.mime.startswith("video/"):
        raise AppError(415, "unsupported_media_type", "MVP nhận video MP4/WebM.")
    if body.size_bytes > settings.max_media_bytes:
        raise AppError(413, "file_too_large", f"Video vượt giới hạn {settings.max_media_bytes // (1024 * 1024)} MB.")
    request_hash = sha256(json.dumps({**body.model_dump(), "filename": filename}, sort_keys=True))
    with db.tx() as conn:
        require_workspace(conn, workspace_id, user)
        if idempotency_key:
            conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{workspace_id}:media:{idempotency_key}",))
            existing = conn.execute("select * from public.upload_session where workspace_id = %s and idempotency_key = %s",
                                    (workspace_id, idempotency_key)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise AppError(409, "idempotency_conflict", "Idempotency-Key đã dùng cho một yêu cầu khác.")
                return session_dto(existing, with_urls=True)
        # Giữ chỗ quota theo kích thước khai báo; complete kiểm lại kích thước thực (mục 4.1).
        used = _media_usage(conn, workspace_id)
        if used + body.size_bytes > settings.media_quota_bytes_per_workspace:
            raise AppError(413, "media_quota_exceeded",
                           f"Workspace còn {(settings.media_quota_bytes_per_workspace - used) // (1024 * 1024)} MB cho media.")
        session_id = str(uuid.uuid4())
        session = conn.execute(
            """insert into public.upload_session (id, workspace_id, owner_id, filename, mime, expected_bytes, part_bytes,
               part_count, object_prefix, language, keep_original, idempotency_key, request_hash, expires_at)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning *""",
            (session_id, workspace_id, user.id, filename, VIDEO_EXTENSIONS[extension], body.size_bytes,
             settings.media_part_bytes, math.ceil(body.size_bytes / settings.media_part_bytes),
             f"{workspace_id}/uploads/{session_id}/", body.language, body.keep_original, idempotency_key, request_hash,
             datetime.now(timezone.utc) + timedelta(seconds=settings.upload_session_ttl_seconds)),
        ).fetchone()
    return session_dto(session, with_urls=True)


@router.get("/api/media-upload-sessions/{session_id}")
def get_media_session(session_id: str, user: UserDep):
    """Trạng thái từng phần + URL mới cho phần còn thiếu (resume sau khi mất mạng)."""
    with db.tx() as conn:
        session = _require_session(conn, session_id, user)
    return session_dto(session, with_urls=True)


@router.post("/api/media-upload-sessions/{session_id}/caption")
async def attach_caption(session_id: str, user: UserDep, file: UploadFile = File(...)):
    settings = get_settings()
    filename = _filename(file.filename, "caption.vtt")
    extension = PurePosixPath(filename).suffix.lower()
    if extension not in CAPTION_EXTENSIONS:
        raise AppError(415, "unsupported_media_type", "Phụ đề phải là SRT hoặc WebVTT.")
    data = await file.read(settings.max_caption_bytes + 1)
    if len(data) > settings.max_caption_bytes:
        raise AppError(413, "file_too_large", "File phụ đề vượt 2 MB.")
    parse_captions(data)  # từ chối sớm file không có cue hợp lệ
    with db.tx() as conn:
        session = _require_session(conn, session_id, user)
        if session["status"] != "open":
            raise AppError(409, "session_closed", "Phiên upload đã đóng.")
        key = f"{session['object_prefix']}caption{extension}"
        store = _media()
        store.delete([key])
        store.put(key, data, CAPTION_EXTENSIONS[extension])
        session = conn.execute(
            "update public.upload_session set caption_object_key = %s, caption_filename = %s where id = %s returning *",
            (key, filename, session["id"])).fetchone()
    return session_dto(session, with_urls=False)


@router.post("/api/media-upload-sessions/{session_id}/complete", status_code=202)
def complete_media_session(session_id: str, user: UserDep):
    """Idempotent: phiên đã complete trả lại nguồn đã tạo."""
    settings = get_settings()
    with db.tx() as conn:
        session = _require_session(conn, session_id, user)
        conn.execute("select id from public.upload_session where id = %s for update", (session["id"],))
        session = conn.execute("select * from public.upload_session where id = %s", (session["id"],)).fetchone()
        if session["status"] == "completed":
            return source_dto(source_row(conn, session["source_id"]))
        if session["status"] != "open" or session["expires_at"] < datetime.now(timezone.utc):
            raise AppError(409, "session_closed", "Phiên upload đã hết hạn hoặc bị hủy.")
        sizes = _uploaded_sizes(session)
        wrong = [i for i in range(session["part_count"]) if sizes.get(i) != _part_size(session, i)]
        if wrong:
            raise AppError(409, "upload_incomplete", f"Còn {len(wrong)} phần chưa tải lên đủ: {wrong[:10]}.")
        if sum(sizes.values()) != session["expected_bytes"]:
            raise AppError(409, "upload_size_mismatch", "Tổng kích thước thực khác kích thước khai báo.")
        workspace_id = str(session["workspace_id"])
        source_id = str(uuid.uuid4())
        alias = conn.execute("select public.next_source_alias(%s) as a", (workspace_id,)).fetchone()["a"]
        title = PurePosixPath(session["filename"]).stem or session["filename"]
        conn.execute(
            """insert into public.source (id, workspace_id, alias, kind, source_type, title, origin_group_id, language,
               capabilities_json) values (%s, %s, %s, 'video', 'user_note', %s, %s, %s, %s)""",
            (source_id, workspace_id, alias, title, f"g-{source_id[:8]}", session["language"],
             db.jsonb({"audio_available": None, "visual_available": None,
                       "captions_available": bool(session["caption_object_key"]), "original_playback_available": None})),
        )
        manifest = {"connector": "upload", "filename": session["filename"], "mime": session["mime"],
                    "expected_bytes": session["expected_bytes"], "part_count": session["part_count"],
                    "object_prefix": session["object_prefix"], "caption": session["caption_filename"],
                    "keep_original": session["keep_original"]}
        conn.execute(
            """insert into public.acquisition (workspace_id, source_id, connector, acquisition_basis, manifest_json,
               manifest_sha256) values (%s, %s, 'upload', 'user_upload', %s, %s)""",
            (workspace_id, source_id, db.jsonb(manifest), sha256(json.dumps(manifest, sort_keys=True))),
        )
        with conn.cursor() as cur:
            cur.executemany(
                """insert into public.media_asset (workspace_id, source_id, kind, object_key, mime, byte_size, meta_json,
                   retention) values (%s, %s, 'original_part', %s, %s, %s, %s, 'staging')""",
                [(workspace_id, source_id, _part_key(session, i), session["mime"], sizes[i], db.jsonb({"index": i}))
                 for i in range(session["part_count"])],
            )
        if session["caption_object_key"]:
            conn.execute(
                """insert into public.media_asset (workspace_id, source_id, kind, object_key, mime, meta_json)
                   values (%s, %s, 'caption', %s, %s, %s)""",
                (workspace_id, source_id, session["caption_object_key"],
                 CAPTION_EXTENSIONS.get(PurePosixPath(session["caption_object_key"]).suffix, "text/plain"),
                 db.jsonb({"filename": session["caption_filename"], "provided_by": "user"})),
            )
        conn.execute(
            """insert into public.job (workspace_id, target_type, target_id, kind, idempotency_key, request_hash, max_attempts)
               values (%s, 'source', %s, 'ingest', %s, %s, %s)""",
            (workspace_id, source_id, f"media-session:{session['id']}", sha256(str(session["id"])), settings.max_job_attempts),
        )
        conn.execute("update public.upload_session set status = 'completed', source_id = %s, completed_at = now() where id = %s",
                     (source_id, session["id"]))
        return source_dto(source_row(conn, source_id))


@router.post("/api/media-upload-sessions/{session_id}/cancel")
def cancel_media_session(session_id: str, user: UserDep):
    with db.tx() as conn:
        session = _require_session(conn, session_id, user)
        if session["status"] != "open":
            raise AppError(409, "session_closed", "Phiên upload đã đóng.")
        conn.execute("update public.upload_session set status = 'cancelled' where id = %s", (session["id"],))
    keys = [_part_key(session, i) for i in range(session["part_count"])]
    if session["caption_object_key"]:
        keys.append(session["caption_object_key"])
    _media().delete(keys)
    return {"status": "cancelled"}


# ----------------------------------------------------------- YouTube / Drive


def youtube_id(url: str) -> str:
    """Chuẩn hóa video ID từ các dạng link phổ biến; không fetch URL (mục 10)."""
    value = url.strip()
    if YOUTUBE_ID.fullmatch(value):
        return value
    patterns = [
        r"^https?://(?:www\.|m\.)?youtube\.com/watch\?(?:.*&)?v=([A-Za-z0-9_-]{11})(?:[&#].*)?$",
        r"^https?://youtu\.be/([A-Za-z0-9_-]{11})(?:[?#].*)?$",
        r"^https?://(?:www\.)?youtube\.com/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})(?:[?#/].*)?$",
    ]
    for pattern in patterns:
        match = re.match(pattern, value)
        if match:
            return match.group(1)
    raise AppError(422, "invalid_youtube_url", "Link YouTube không hợp lệ.")


@router.post("/api/workspaces/{workspace_id}/sources/youtube", status_code=202)
async def add_youtube(workspace_id: str, user: UserDep, url: Annotated[str, Form()],
                      language: Annotated[Literal["vi", "en", "auto"], Form()] = "auto",
                      title: Annotated[str | None, Form()] = None,
                      caption: UploadFile | None = File(None)):
    """Nguồn chỉ có phụ đề: người dùng cung cấp SRT/VTT họ được phép dùng (mục 3).

    Lấy track qua YouTube Data API cần OAuth và quyền chỉnh sửa video (captions.download) — chưa cấu hình,
    nên không có caption thì trả 501 thay vì tự tải video/phụ đề bằng cơ chế khác.
    """
    settings = get_settings()
    video_id = youtube_id(url)
    if caption is None:
        raise AppError(501, "youtube_oauth_not_configured",
                       "Chưa kết nối YouTube API (OAuth). Hãy tải lên file phụ đề SRT/VTT bạn được phép sử dụng cho video này.")
    filename = _filename(caption.filename, "caption.vtt")
    extension = PurePosixPath(filename).suffix.lower()
    if extension not in CAPTION_EXTENSIONS:
        raise AppError(415, "unsupported_media_type", "Phụ đề phải là SRT hoặc WebVTT.")
    data = await caption.read(settings.max_caption_bytes + 1)
    if len(data) > settings.max_caption_bytes:
        raise AppError(413, "file_too_large", "File phụ đề vượt 2 MB.")
    parse_captions(data)
    source_id = str(uuid.uuid4())
    key = f"{workspace_id}/{source_id}/caption{extension}"
    with db.tx() as conn:
        require_workspace(conn, workspace_id, user)
        _media().put(key, data, CAPTION_EXTENSIONS[extension])
        alias = conn.execute("select public.next_source_alias(%s) as a", (workspace_id,)).fetchone()["a"]
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        conn.execute(
            """insert into public.source (id, workspace_id, alias, kind, source_type, title, url, origin_group_id, language,
               capabilities_json) values (%s, %s, %s, 'youtube', 'web', %s, %s, %s, %s, %s)""",
            (source_id, workspace_id, alias, (title or f"YouTube {video_id}")[:300], watch_url, f"yt-{video_id}", language,
             db.jsonb({"audio_available": False, "visual_available": False, "captions_available": True,
                       "original_playback_available": "external"})),
        )
        manifest = {"connector": "youtube", "video_id": video_id, "caption": filename, "caption_origin": "user_provided"}
        conn.execute(
            """insert into public.acquisition (workspace_id, source_id, connector, provider_id, acquisition_basis,
               manifest_json, manifest_sha256) values (%s, %s, 'youtube', %s, 'user_provided_caption', %s, %s)""",
            (workspace_id, source_id, video_id, db.jsonb(manifest), sha256(json.dumps(manifest, sort_keys=True))),
        )
        conn.execute(
            """insert into public.media_asset (workspace_id, source_id, kind, object_key, mime, byte_size, meta_json)
               values (%s, %s, 'caption', %s, %s, %s, %s)""",
            (workspace_id, source_id, key, CAPTION_EXTENSIONS[extension], len(data),
             db.jsonb({"filename": filename, "provided_by": "user"})),
        )
        conn.execute(
            """insert into public.job (workspace_id, target_type, target_id, kind, max_attempts)
               values (%s, 'source', %s, 'ingest', %s)""", (workspace_id, source_id, settings.max_job_attempts))
        return source_dto(source_row(conn, source_id))


@router.post("/api/workspaces/{workspace_id}/sources/drive")
def add_drive(workspace_id: str, user: UserDep):
    with db.tx() as conn:
        require_workspace(conn, workspace_id, user)
    raise AppError(501, "drive_oauth_not_configured",
                   "Chưa kết nối Google Drive (OAuth + Drive API). Tạm thời hãy tải file video về rồi upload.")


# --------------------------------------------------------- timeline / media


def _require_live_source(conn, source_id: str, user) -> dict:
    source = require_owned(conn, "source", source_id, user, "Nguồn")
    if source["deleted_at"]:
        raise AppError(410, "source_deleted", "Nguồn đã bị xóa; nội dung không còn được hiển thị.")
    return source


@router.get("/api/sources/{source_id}/timeline")
def source_timeline(source_id: str, user: UserDep, revision_id: str):
    """Transcript + vùng chữ + asset của đúng parse revision (citation mở đúng revision đã dùng)."""
    with db.tx() as conn:
        source = _require_live_source(conn, source_id, user)
        extraction = conn.execute(
            "select * from public.extraction_revision where parse_revision_id = %s and source_id = %s",
            (_uuid(revision_id, "Phiên bản"), source["id"])).fetchone()
        if not extraction:
            raise not_found("Timeline của phiên bản")
        segments = conn.execute(
            "select * from public.transcript_segment where extraction_revision_id = %s order by ordinal", (extraction["id"],)).fetchall()
        regions = conn.execute(
            "select * from public.frame_region where extraction_revision_id = %s order by ordinal", (extraction["id"],)).fetchall()
        assets = conn.execute(
            """select id, kind, mime, meta_json from public.media_asset
               where source_id = %s and kind in ('audio', 'frame') and status = 'ready' order by kind, created_at""",
            (source["id"],)).fetchall()
    return {
        "source_id": str(source["id"]),
        "parse_revision_id": revision_id,
        "extraction_revision_id": str(extraction["id"]),
        "parent_extraction_revision_id": str(extraction["parent_revision_id"]) if extraction["parent_revision_id"] else None,
        "duration_ms": source["duration_ms"],
        "capabilities": source["capabilities_json"],
        "coverage": extraction["coverage_json"],
        "versions": extraction["versions_json"],
        "segments": [{"id": str(s["id"]), "start_ms": s["start_ms"], "end_ms": s["end_ms"], "text": s["text"],
                      "origin": s["origin"], "flags": (s["quality_json"] or {}).get("flags", []),
                      "reviewed": bool((s["quality_json"] or {}).get("reviewed")),
                      "audio_asset_id": str(s["audio_asset_id"]) if s["audio_asset_id"] else None} for s in segments],
        "regions": [{"id": str(r["id"]), "frame_asset_id": str(r["frame_asset_id"]), "timestamp_ms": r["timestamp_ms"],
                     "visible_from_ms": r["visible_from_ms"], "visible_to_ms": r["visible_to_ms"], "bbox": r["bbox_json"],
                     "text": r["text"], "method": r["method"], "confidence": r["confidence"],
                     "included": bool((r["quality_json"] or {}).get("included", True)),
                     "flags": (r["quality_json"] or {}).get("flags", []), "review_state": r["review_state"]} for r in regions],
        "assets": [{"id": str(a["id"]), "kind": a["kind"], "mime": a["mime"], "meta": a["meta_json"]} for a in assets],
    }


@router.get("/api/sources/{source_id}/media/{asset_id}")
def media_url(source_id: str, asset_id: str, user: UserDep):
    """URL đọc hạn ngắn cho audio/frame/video gốc (nếu được giữ), cấp sau kiểm tra quyền + tombstone (mục 6)."""
    with db.tx() as conn:
        source = _require_live_source(conn, source_id, user)
        asset = conn.execute(
            """select * from public.media_asset where id = %s and source_id = %s and status = 'ready'
               and kind in ('audio', 'frame', 'crop', 'original_part')""",
            (_uuid(asset_id, "Media"), source["id"])).fetchone()
    if not asset:
        raise not_found("Media")
    expires = 300
    return {"asset_id": str(asset["id"]), "kind": asset["kind"], "mime": asset["mime"], "meta": asset["meta_json"],
            "url": _media().signed_url(asset["object_key"], expires), "expires_in": expires}


# ----------------------------------------------------------------- sửa / reindex


class TextEdit(BaseModel):
    id: str
    text: str = Field(min_length=1, max_length=5000)


class TranscriptRevisionIn(BaseModel):
    base_revision_id: str                  # parse revision đang xem
    segments: list[TextEdit] = Field(default_factory=list, max_length=2000)
    regions: list[TextEdit] = Field(default_factory=list, max_length=2000)


@router.post("/api/sources/{source_id}/transcript-revisions", status_code=202)
def create_transcript_revision(source_id: str, body: TranscriptRevisionIn, user: UserDep):
    """Sửa bản chép/OCR → revision trích xuất + parse revision + index mới; run cũ giữ revision cũ (mục 6, bất biến 4)."""
    if not body.segments and not body.regions:
        raise AppError(422, "empty_edit", "Không có thay đổi nào.")
    with db.tx() as conn:
        source = _require_live_source(conn, source_id, user)
        extraction = conn.execute(
            "select * from public.extraction_revision where parse_revision_id = %s and source_id = %s",
            (_uuid(body.base_revision_id, "Phiên bản"), source["id"])).fetchone()
        if not extraction:
            raise not_found("Phiên bản trích xuất")
        segment_ids = {str(r["id"]) for r in conn.execute(
            "select id from public.transcript_segment where extraction_revision_id = %s", (extraction["id"],))}
        region_ids = {str(r["id"]) for r in conn.execute(
            "select id from public.frame_region where extraction_revision_id = %s", (extraction["id"],))}
        unknown = [e.id for e in body.segments if e.id not in segment_ids] + [e.id for e in body.regions if e.id not in region_ids]
        if unknown:
            raise AppError(422, "unknown_items", f"{len(unknown)} mục không thuộc phiên bản này.")
        busy = conn.execute(
            """select 1 from public.job where target_id = %s and kind in ('ingest', 'reindex') and state in ('queued', 'running')""",
            (source["id"],)).fetchone()
        if busy:
            raise AppError(409, "source_busy", "Nguồn đang được xử lý; thử lại sau khi hoàn tất.")
        payload = {"action": "transcript_edit", "base_extraction_revision_id": str(extraction["id"]), "user_id": user.id,
                   "segments": {e.id: e.text for e in body.segments}, "regions": {e.id: e.text for e in body.regions}}
        job = conn.execute(
            """insert into public.job (workspace_id, target_type, target_id, kind, payload_json, max_attempts)
               values (%s, 'source', %s, 'reindex', %s, %s) returning *""",
            (source["workspace_id"], source["id"], db.jsonb(payload), get_settings().max_job_attempts)).fetchone()
    return job_dto(job)


class ReindexIn(BaseModel):
    profile: Literal["product", "lecture_vi_v1"]


@router.post("/api/sources/{source_id}/reindex", status_code=202)
def reindex_source(source_id: str, body: ReindexIn, user: UserDep):
    """Thêm generation cho profile khác (vd. đưa PDF vào phạm vi hỏi cùng bài giảng); không đổi index đang dùng."""
    if body.profile not in PROFILE_NAMES:
        raise AppError(422, "unknown_profile", "Profile không tồn tại.")
    with db.tx() as conn:
        source = _require_live_source(conn, source_id, user)
        if source["status"] not in ("ready", "ready_limited", "review_required"):
            raise AppError(409, "source_not_ready", "Nguồn chưa sẵn sàng để reindex.")
        job = conn.execute(
            """insert into public.job (workspace_id, target_type, target_id, kind, payload_json, max_attempts)
               values (%s, 'source', %s, 'reindex', %s, %s) returning *""",
            (source["workspace_id"], source["id"], db.jsonb({"action": "profile", "profile": body.profile}),
             get_settings().max_job_attempts)).fetchone()
    return job_dto(job)



