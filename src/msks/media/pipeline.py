"""Điều phối nạp bài giảng trong worker (LECTURE_ARCHITECTURE.md mục 5, 8).

ACQUIRE → PROBE → EXTRACT (audio/frame) → TRANSCRIBE (caption | ASR) → VISUAL_READ (OCR) → ALIGN
→ BUILD_CANONICAL → EMBED → PUBLISH. Mọi ghi DB đi qua `jobs.guarded`. Kết quả trích xuất đã thành
công được tái sử dụng khi job chạy lại (chỉ dựng lại canonical/index), không xử lý lại cả video.
"""
from __future__ import annotations

import hashlib
import logging
import math
import tempfile
import uuid
from pathlib import Path

from edahr.text import normalize, token_set

from .. import db, jobs
from ..errors import PermanentError
from ..indexing import LeafRow, check_leaf_quota, embed_and_publish, persist_tree
from ..ingest import build_tree
from ..ml import get_models
from ..profiles import get_profile
from ..settings import get_settings
from ..storage import BlobStore
from . import asr, captions, frames, ocr, probe
from .timeline import (
    ALIGNMENT_POLICY,
    SECTION_POLICY,
    BoardItem,
    SpeechItem,
    alignment_edges,
    build_document,
    coverage,
    node_extras,
    plan_sections,
)

log = logging.getLogger("msks.media")
PROFILE = "lecture_vi_v1"
SAMPLE_MAX_WIDTH = 1280


def _progress(job: dict, source_id: str, status: str, progress: float) -> None:
    with jobs.guarded(job) as conn:
        conn.execute("update public.source set status = %s, progress = %s where id = %s and deleted_at is null",
                     (status, progress, source_id))


def _versions(settings, language: str | None, captions_used: bool, glossary: str | None) -> dict:
    model = settings.asr_model_vi if language == "vi" and settings.asr_model_vi else settings.asr_model
    return {
        "asr": None if captions_used else {"model": model, "compute_type": settings.asr_compute_type,
                                           "vad": True, "condition_on_previous_text": False,
                                           "glossary_sha256": hashlib.sha256(glossary.encode()).hexdigest()[:16]
                                           if glossary else None},
        "ocr": {"engine": ocr.engine_version(settings.ocr_engine), "min_confidence": ocr.MIN_CONFIDENCE[settings.ocr_engine]},
        "sampler": {"interval_s": settings.frame_sample_seconds, "threshold": settings.frame_change_threshold,
                    "max_frames_per_hour": settings.max_frames_per_hour},
        "section_policy": SECTION_POLICY,
        "alignment_policy": ALIGNMENT_POLICY,
        "profile": get_profile(PROFILE).digest(),
    }


def _upload_asset(job: dict, store: BlobStore, source: dict, revision_id: str, kind: str, key: str, path: Path,
                  mime: str, meta: dict) -> str:
    store.put_file(key, path, mime)
    asset_id = str(uuid.uuid4())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with jobs.guarded(job) as conn:
        conn.execute(
            """insert into public.media_asset (id, workspace_id, source_id, document_revision_id, kind, object_key, mime,
               sha256, byte_size, meta_json, retention) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'source')""",
            (asset_id, source["workspace_id"], source["id"], revision_id, kind, key, mime, digest, path.stat().st_size,
             db.jsonb(meta)),
        )
    return asset_id


# ----------------------------------------------------------------- acquire


def _acquire(job: dict, source: dict, folder: Path) -> tuple[dict, Path | None, bytes | None]:
    """Ghép các phần video (hoặc đọc caption) → document_revision. Trả (revision, video path, caption bytes)."""
    store = BlobStore(get_settings().media_bucket)
    with jobs.guarded(job) as conn:
        acquisition = conn.execute(
            "select * from public.acquisition where source_id = %s order by acquired_at desc limit 1", (source["id"],)
        ).fetchone()
        assets = conn.execute(
            "select * from public.media_asset where source_id = %s and kind in ('original_part', 'caption') and status = 'ready'",
            (source["id"],),
        ).fetchall()
        revision = conn.execute(
            "select * from public.document_revision where source_id = %s order by revision_no desc limit 1", (source["id"],)
        ).fetchone()
    if not acquisition:
        raise PermanentError("acquisition_missing", "Nguồn không có thông tin acquisition.")
    manifest = acquisition["manifest_json"]
    caption = next((a for a in assets if a["kind"] == "caption"), None)
    caption_bytes = store.get(caption["object_key"]) if caption else None
    parts = sorted((a for a in assets if a["kind"] == "original_part"), key=lambda a: a["meta_json"]["index"])

    video: Path | None = None
    digest = hashlib.sha256()
    size = 0
    if parts:
        video = folder / ("original" + Path(manifest.get("filename", "video.mp4")).suffix.lower())
        with open(video, "wb") as out:
            for part in parts:
                part_path = folder / f"part-{part['meta_json']['index']:04d}"
                store.download_to(part["object_key"], part_path)
                data = part_path.read_bytes()
                out.write(data)
                digest.update(data)
                size += len(data)
                part_path.unlink()
        if size != int(manifest.get("expected_bytes", size)):
            raise PermanentError("upload_size_mismatch", f"Tổng kích thước các phần ({size} B) khác kích thước khai báo.")
    elif caption_bytes is not None:
        digest.update(caption_bytes)
        size = len(caption_bytes)
    else:
        raise PermanentError("media_missing", "Không có video hoặc phụ đề để xử lý.")

    if not revision:
        with jobs.guarded(job) as conn:
            revision = conn.execute(
                """insert into public.document_revision (workspace_id, source_id, revision_no, mime, object_key, sha256,
                   byte_size, license_metadata_json) values (%s, %s, 1, %s, %s, %s, %s, %s) returning *""",
                (source["workspace_id"], source["id"], manifest.get("mime", "application/octet-stream"),
                 manifest.get("object_prefix") or (caption or {}).get("object_key"), digest.hexdigest(), size,
                 db.jsonb({"connector": acquisition["connector"], "basis": acquisition["acquisition_basis"],
                           "provider_id": acquisition["provider_id"]})),
            ).fetchone()
            conn.execute(
                "update public.media_asset set document_revision_id = %s where source_id = %s and document_revision_id is null",
                (revision["id"], source["id"]),
            )
            conn.execute("update public.acquisition set status = 'acquired' where id = %s", (acquisition["id"],))
    elif revision["sha256"] != digest.hexdigest():
        raise PermanentError("checksum_mismatch", "Bytes nhận lại khác checksum đã ghi; không xử lý tiếp.")
    return revision, video, caption_bytes


# ------------------------------------------------------------------ ingest


def ingest_media(job: dict, source: dict) -> None:
    settings = get_settings()
    source_id = str(source["id"])
    with jobs.guarded(job) as conn:
        done = conn.execute(
            """select e.id from public.extraction_revision e where e.source_id = %s and e.status = 'succeeded'
               order by e.created_at desc limit 1""", (source_id,),
        ).fetchone()
    if done:
        # Trích xuất đã xong ở attempt trước: chỉ dựng lại canonical + index, không chạy lại ASR/OCR.
        rebuild(job, source, str(done["id"]))
        return

    with tempfile.TemporaryDirectory(prefix="msks-media-") as tmp:
        folder = Path(tmp)
        _progress(job, source_id, "parsing", 0.05)
        jobs.set_stage(job, "acquire")
        revision, video, caption_bytes = _acquire(job, source, folder)
        revision_id = str(revision["id"])
        language = source["language"] or "auto"
        with jobs.guarded(job) as conn:
            workspace_settings = conn.execute("select settings_json from public.workspace where id = %s",
                                              (source["workspace_id"],)).fetchone()["settings_json"] or {}
        glossary = asr.glossary_prompt(workspace_settings.get("asr_glossary"), settings.asr_glossary)

        info = None
        if video is not None:
            jobs.set_stage(job, "probe")
            info = probe.probe(video)
            if info.duration_ms > settings.max_media_seconds * 1000:
                raise PermanentError("media_too_long", f"Video dài {info.duration_ms // 60000} phút, vượt giới hạn "
                                                       f"{settings.max_media_seconds // 60} phút.")
            if not info.has_audio and not info.has_video:
                raise PermanentError("media_unreadable", "File không có luồng âm thanh hay hình ảnh.")
        duration = info.duration_ms if info else None
        timeout = max(300.0, (duration or 0) / 1000 * 2)

        with jobs.guarded(job) as conn:
            # Dọn kết quả dở của attempt trước (chưa từng publish) rồi mở revision trích xuất mới.
            conn.execute("update public.extraction_revision set status = 'failed' where source_id = %s and status = 'running'",
                         (source_id,))
            conn.execute(
                """update public.media_asset set status = 'deleted', retain_until = now()
                   where source_id = %s and kind in ('audio', 'frame') and status = 'ready'""", (source_id,))
            extraction_id = str(conn.execute(
                """insert into public.extraction_revision (workspace_id, source_id, document_revision_id, profile,
                   input_hashes_json, versions_json) values (%s, %s, %s, %s, %s, %s) returning id""",
                (source["workspace_id"], source_id, revision_id, PROFILE,
                 db.jsonb({"document_sha256": revision["sha256"]}),
                 db.jsonb(_versions(settings, language, caption_bytes is not None, glossary))),
            ).fetchone()["id"])
            if duration is not None:
                conn.execute("update public.source set duration_ms = %s where id = %s", (duration, source_id))

        store = BlobStore(settings.media_bucket)
        base_key = f"{source['workspace_id']}/{source_id}/{revision_id}"

        # ---- EXTRACT: âm thanh bằng chứng (FLAC theo đoạn) + WAV cho ASR
        audio_chunks: list[tuple[str, int, int]] = []
        wav = None
        if info and info.has_audio:
            jobs.set_stage(job, "extract_audio")
            _progress(job, source_id, "parsing", 0.15)
            wav, chunks = probe.extract_audio(video, folder, settings.audio_chunk_seconds, timeout)
            for index, (path, start, end) in enumerate(chunks):
                asset = _upload_asset(job, store, source, revision_id, "audio", f"{base_key}/audio/{index:03d}.flac", path,
                                      "audio/flac", {"chunk_index": index, "start_ms": start, "end_ms": end,
                                                     "sample_rate": 16000, "channels": 1, "codec": "flac"})
                audio_chunks.append((asset, start, end))

        def audio_for(ms: int) -> str | None:
            return next((a for a, s, e in audio_chunks if s <= ms < e), audio_chunks[-1][0] if audio_chunks else None)

        # ---- TRANSCRIBE: caption hợp lệ ưu tiên; không có → ASR
        speech: list[SpeechItem] = []
        dropped = 0
        track = None
        if caption_bytes is not None:
            jobs.set_stage(job, "captions")
            cues = captions.validate_timing(captions.parse_captions(caption_bytes), duration)
            track = "caption:user_provided"
            speech = [SpeechItem(str(uuid.uuid4()), normalize(c.text), c.start_ms, c.end_ms, "human_caption",
                                 c.flags, False, audio_for(c.start_ms)) for c in cues if normalize(c.text)]
        elif wav is not None:
            jobs.set_stage(job, "transcribe")
            _progress(job, source_id, "parsing", 0.3)
            model = settings.asr_model_vi if language == "vi" and settings.asr_model_vi else settings.asr_model
            result = asr.transcribe(wav, model, get_models().device, settings.asr_compute_type, language, glossary,
                                    info.duration_ms if info else None)
            get_models().free_gpu_cache()
            dropped = result.dropped
            track = f"asr:{result.model}"
            speech = [SpeechItem(str(uuid.uuid4()), normalize(s.text), s.start_ms, s.end_ms, "asr", s.flags, False,
                                 audio_for(s.start_ms)) for s in result.segments if normalize(s.text)]
            if language == "auto" and result.language:
                language = result.language

        # ---- VISUAL_READ: frame đổi nội dung → OCR → khử gần trùng → giữ frame làm bằng chứng
        board: list[BoardItem] = []
        frames_kept = 0
        incomplete_from = None
        if info and info.has_video:
            jobs.set_stage(job, "visual_read")
            _progress(job, source_id, "chunking", 0.5)
            samples = probe.sample_frames(video, folder, settings.frame_sample_seconds, SAMPLE_MAX_WIDTH, timeout,
                                            info.start_time_ms)
            budget = max(1, math.ceil(info.duration_ms / 3_600_000 * settings.max_frames_per_hour))
            selected, incomplete_from = frames.select_frames(samples, info.duration_ms, settings.frame_change_threshold, budget)
            scale = min(1.0, SAMPLE_MAX_WIDTH / info.width) if info.width else 1.0
            min_conf = ocr.MIN_CONFIDENCE[settings.ocr_engine]
            previous_tokens: set[str] | None = None
            previous_items: list[BoardItem] = []
            for frame in selected:
                regions = ocr.read_frame(frame.path, scale, settings.ocr_engine, language, gpu=get_models().device != "cpu")
                tokens = token_set(" ".join(r.text for r in regions))
                if previous_tokens is not None and tokens and previous_tokens and (
                        len(tokens & previous_tokens) / max(1, len(tokens | previous_tokens))) >= 0.9:
                    for item in previous_items:  # cùng nội dung: kéo dài khoảng hiển thị, không OCR trùng
                        item.visible_to_ms = frame.visible_to_ms
                    continue
                if not regions:
                    continue
                frames_kept += 1
                asset = _upload_asset(job, store, source, revision_id, "frame",
                                      f"{base_key}/frames/{frame.timestamp_ms:09d}.jpg", frame.path, "image/jpeg",
                                      {"timestamp_ms": frame.timestamp_ms, "scale": scale,
                                       "width": info.width, "height": info.height})
                previous_items = []
                for region in regions:
                    included = region.confidence >= min_conf
                    item = BoardItem(str(uuid.uuid4()), asset, frame.timestamp_ms, frame.timestamp_ms, frame.visible_to_ms,
                                     region.bbox, normalize(region.text), region.confidence,
                                     [] if included else ["low_confidence"], False, included)
                    board.append(item)
                    previous_items.append(item)
                previous_tokens = tokens
            ocr.release()
            get_models().free_gpu_cache()

        if not speech and not any(b.included for b in board):
            raise PermanentError("no_extractable_content", "Không trích được lời giảng hay chữ trên bảng nào từ nguồn.")

        cov = coverage(speech, board, duration, frames_selected=frames_kept, visual_incomplete_from=incomplete_from,
                       dropped_segments=dropped, has_audio=bool(info and info.has_audio),
                       has_video=bool(info and info.has_video), captions_used=caption_bytes is not None)
        cov["has_audio"] = bool(info and info.has_audio)
        jobs.set_stage(job, "align")
        with jobs.guarded(job) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """insert into public.transcript_segment (id, workspace_id, extraction_revision_id, track, ordinal, text,
                       language, start_ms, end_ms, origin, quality_json, audio_asset_id)
                       values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [(s.id, source["workspace_id"], extraction_id, track, i, s.text, language, s.start_ms, s.end_ms,
                      s.origin, db.jsonb({"flags": s.flags, "reviewed": s.reviewed}), s.audio_asset_id)
                     for i, s in enumerate(speech)],
                )
                cur.executemany(
                    """insert into public.frame_region (id, workspace_id, extraction_revision_id, frame_asset_id, ordinal,
                       timestamp_ms, visible_from_ms, visible_to_ms, bbox_json, text, method, confidence, quality_json)
                       values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ocr', %s, %s)""",
                    [(b.id, source["workspace_id"], extraction_id, b.frame_asset_id, i, b.timestamp_ms, b.visible_from_ms,
                      b.visible_to_ms, db.jsonb(b.bbox), b.text, b.confidence,
                      db.jsonb({"flags": b.flags, "included": b.included})) for i, b in enumerate(board)],
                )
                cur.executemany(
                    """insert into public.alignment_edge (workspace_id, extraction_revision_id, segment_id, region_id,
                       relation, score, policy_version) values (%s, %s, %s, %s, 'during', %s, %s)""",
                    [(source["workspace_id"], extraction_id, seg, reg, score, ALIGNMENT_POLICY)
                     for seg, reg, score in alignment_edges(speech, board)],
                )
            conn.execute(
                "update public.extraction_revision set status = 'succeeded', coverage_json = %s where id = %s",
                (db.jsonb(cov), extraction_id),
            )
            if language != (source["language"] or "auto"):
                conn.execute("update public.source set language = %s where id = %s and language = 'auto'",
                             (language if language in ("vi", "en") else "auto", source_id))
    rebuild(job, source, extraction_id)


# ----------------------------------------------------------------- rebuild


def _load_items(conn, extraction_id: str) -> tuple[list[SpeechItem], list[BoardItem]]:
    speech = [
        SpeechItem(str(r["id"]), r["text"], r["start_ms"], r["end_ms"], r["origin"],
                   list((r["quality_json"] or {}).get("flags", [])), bool((r["quality_json"] or {}).get("reviewed")),
                   str(r["audio_asset_id"]) if r["audio_asset_id"] else None)
        for r in conn.execute("select * from public.transcript_segment where extraction_revision_id = %s order by ordinal",
                              (extraction_id,))
    ]
    board = [
        BoardItem(str(r["id"]), str(r["frame_asset_id"]), r["timestamp_ms"], r["visible_from_ms"], r["visible_to_ms"],
                  r["bbox_json"], r["text"], float(r["confidence"] or 0), list((r["quality_json"] or {}).get("flags", [])),
                  r["review_state"] == "reviewed",
                  bool((r["quality_json"] or {}).get("included", True)) and r["review_state"] != "rejected")
        for r in conn.execute("select * from public.frame_region where extraction_revision_id = %s order by ordinal",
                              (extraction_id,))
    ]
    return speech, board


def final_status(cov: dict, capabilities: dict, review_ratio: float) -> str:
    """READY_LIMITED khi thiếu nhánh tùy chọn; REVIEW_REQUIRED khi bản chép quá nhiều đoạn bị cảnh báo (mục 8)."""
    ratio = cov.get("flagged_ratio")
    if ratio is not None and ratio > review_ratio:
        return "review_required"
    limited = (not capabilities["visual_available"] or not (capabilities["audio_available"] or capabilities["captions_available"])
               or cov.get("visual_incomplete_from_ms") is not None)
    return "ready_limited" if limited else "ready"


def rebuild(job: dict, source: dict, extraction_id: str) -> None:
    """Dựng canonical + source map + cây node từ một extraction revision rồi index theo profile bài giảng."""
    settings = get_settings()
    source_id = str(source["id"])
    with jobs.guarded(job) as conn:
        extraction = conn.execute("select * from public.extraction_revision where id = %s", (extraction_id,)).fetchone()
        speech, board = _load_items(conn, extraction_id)
        current = conn.execute("select * from public.source where id = %s", (source_id,)).fetchone()
    cov = extraction["coverage_json"] or {}
    jobs.set_stage(job, "build_canonical")
    _progress(job, source_id, "chunking", 0.7)
    slide_starts = sorted({b.timestamp_ms for b in board if b.included})
    plans = plan_sections(speech, board, slide_starts, settings.speech_window_seconds * 1000)
    document, spans, canonical = build_document(plans, str(extraction["document_revision_id"]), current["alias"],
                                                current["language"])
    tree = build_tree(document, settings, False, "msks-lecture", SECTION_POLICY)
    if tree.canonical_text != canonical:
        raise PermanentError("offset_mismatch", "Canonical text không khớp quy ước offset (lỗi nội bộ).")
    extras = node_extras(tree.nodes, spans)
    check_leaf_quota(job, str(source["workspace_id"]), len(tree.leaves))

    def write_source_map(conn, parse_revision_id: str, _ids: dict[str, str]) -> None:
        with conn.cursor() as cur:
            cur.executemany(
                """insert into public.source_map_span (workspace_id, parse_revision_id, char_start, char_end, target_type,
                   target_id, target_char_start, target_char_end, start_ms, end_ms, asset_id, bbox_json, precision)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(source["workspace_id"], parse_revision_id, s.char_start, s.char_end, s.target_type, s.target_id,
                  s.target_char_start, s.target_char_end, s.start_ms, s.end_ms, s.asset_id,
                  db.jsonb(s.bbox) if s.bbox else None, s.precision) for s in spans],
            )
        conn.execute("update public.extraction_revision set parse_revision_id = %s where id = %s",
                     (parse_revision_id, extraction_id))

    parse_revision_id, ids = persist_tree(job, str(source["workspace_id"]), str(extraction["document_revision_id"]), tree,
                                          node_extra=extras, after_insert=write_source_map)
    capabilities = {
        "audio_available": bool(cov.get("has_audio")),
        "visual_available": bool(cov.get("visual_read")),
        "captions_available": bool(cov.get("captions_used")),
        "original_playback_available": "external" if current["kind"] == "youtube" else (
            "kept" if _keep_original(source_id) else "audio_and_frames"),
    }
    status = final_status(cov, capabilities, settings.review_low_confidence_ratio)
    jobs.set_stage(job, "embedding")
    _progress(job, source_id, "embedding", 0.85)

    def after_publish(conn) -> None:
        # Supersede revision trích xuất cũ; hẹn xóa video gốc nếu người dùng không chọn giữ (mục 4.2).
        conn.execute(
            """update public.extraction_revision set status = 'superseded'
               where source_id = %s and status = 'succeeded' and id <> %s and parse_revision_id is not null""",
            (source_id, extraction_id))
        if not capabilities["original_playback_available"] == "kept":
            conn.execute(
                """update public.media_asset set retention = 'staging',
                       retain_until = coalesce(retain_until, now() + make_interval(secs => %s))
                   where source_id = %s and kind = 'original_part' and status = 'ready'""",
                (settings.original_grace_seconds, source_id))

    rows = [LeafRow(ids[n.legacy_node_id], n.text, n.embedding_text) for n in tree.leaves]
    embed_and_publish(job, source_id, str(source["workspace_id"]), parse_revision_id, rows, PROFILE,
                      final_status=status,
                      source_updates={"capabilities_json": capabilities, "coverage_json": cov},
                      on_published=after_publish)


def _keep_original(source_id: str) -> bool:
    with db.tx() as conn:
        row = conn.execute("select keep_original from public.upload_session where source_id = %s", (source_id,)).fetchone()
    return bool(row and row["keep_original"])


# ------------------------------------------------------------ transcript edit


def apply_edits(job: dict, source: dict, base_extraction_id: str, segment_edits: dict[str, str],
                region_edits: dict[str, str], user_id: str | None) -> str:
    """Revision trích xuất mới = bản cũ + sửa của người dùng (origin manual_edit, đã đối chiếu). Không ghi đè bản cũ."""
    with jobs.guarded(job) as conn:
        base = conn.execute("select * from public.extraction_revision where id = %s and source_id = %s",
                            (base_extraction_id, source["id"])).fetchone()
        if not base:
            raise PermanentError("extraction_not_found", "Không tìm thấy revision trích xuất gốc.")
        speech, board = _load_items(conn, base_extraction_id)
        new_id = str(conn.execute(
            """insert into public.extraction_revision (workspace_id, source_id, document_revision_id, parent_revision_id,
               profile, input_hashes_json, versions_json, coverage_json, status, created_by)
               values (%s, %s, %s, %s, %s, %s, %s, %s, 'running', %s) returning id""",
            (source["workspace_id"], source["id"], base["document_revision_id"], base_extraction_id, base["profile"],
             db.jsonb(base["input_hashes_json"]), db.jsonb({**(base["versions_json"] or {}), "edits": len(segment_edits) + len(region_edits)}),
             db.jsonb(base["coverage_json"]), user_id),
        ).fetchone()["id"])
        id_map: dict[str, str] = {}
        segments = []
        for i, s in enumerate(speech):
            new = str(uuid.uuid4())
            id_map[s.id] = new
            edited = s.id in segment_edits
            text = normalize(segment_edits[s.id]) if edited else s.text
            flags = [] if edited else s.flags
            segments.append((new, source["workspace_id"], new_id, f"derived:{base_extraction_id}", i, text,
                             source["language"], s.start_ms, s.end_ms, "manual_edit" if edited else s.origin,
                             db.jsonb({"flags": flags, "reviewed": edited or s.reviewed, "edited_from": s.id if edited else None}),
                             s.audio_asset_id))
        regions = []
        for i, b in enumerate(board):
            new = str(uuid.uuid4())
            id_map[b.id] = new
            edited = b.id in region_edits
            regions.append((new, source["workspace_id"], new_id, b.frame_asset_id, i, b.timestamp_ms, b.visible_from_ms,
                            b.visible_to_ms, db.jsonb(b.bbox), normalize(region_edits[b.id]) if edited else b.text,
                            "manual_edit" if edited else "ocr", b.confidence,
                            db.jsonb({"flags": [] if edited else b.flags, "included": True if edited else b.included}),
                            "reviewed" if edited or b.reviewed else "unreviewed"))
        with conn.cursor() as cur:
            cur.executemany(
                """insert into public.transcript_segment (id, workspace_id, extraction_revision_id, track, ordinal, text,
                   language, start_ms, end_ms, origin, quality_json, audio_asset_id)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", segments)
            cur.executemany(
                """insert into public.frame_region (id, workspace_id, extraction_revision_id, frame_asset_id, ordinal,
                   timestamp_ms, visible_from_ms, visible_to_ms, bbox_json, text, method, confidence, quality_json,
                   review_state) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", regions)
        new_speech, new_board = _load_items(conn, new_id)
        cov = {**(base["coverage_json"] or {}), **{k: v for k, v in coverage(
            new_speech, new_board, (base["coverage_json"] or {}).get("duration_ms"),
            frames_selected=(base["coverage_json"] or {}).get("frames_selected", 0),
            visual_incomplete_from=(base["coverage_json"] or {}).get("visual_incomplete_from_ms"),
            dropped_segments=(base["coverage_json"] or {}).get("segments_dropped_as_non_speech", 0),
            has_audio=bool((base["coverage_json"] or {}).get("audio_read")),
            has_video=bool((base["coverage_json"] or {}).get("visual_read")),
            captions_used=bool((base["coverage_json"] or {}).get("captions_used"))).items()
            if k in ("segments", "segments_flagged", "flagged_ratio", "board_regions", "board_regions_low_confidence")}}
        with conn.cursor() as cur:
            cur.executemany(
                """insert into public.alignment_edge (workspace_id, extraction_revision_id, segment_id, region_id, relation,
                   score, policy_version) values (%s, %s, %s, %s, 'during', %s, %s)""",
                [(source["workspace_id"], new_id, seg, reg, score, ALIGNMENT_POLICY)
                 for seg, reg, score in alignment_edges(new_speech, new_board)])
        conn.execute("update public.extraction_revision set status = 'succeeded', coverage_json = %s where id = %s",
                     (db.jsonb(cov), new_id))
    return new_id


# --------------------------------------------------------------------- GC


def delete_media(source_id: str) -> None:
    """GC nguồn bài giảng: xóa mọi asset media, che nội dung bản chép/OCR (mục 4.2, bất biến 6)."""
    settings = get_settings()
    store = BlobStore(settings.media_bucket)
    with db.tx() as conn:
        keys = [r["object_key"] for r in conn.execute(
            "select object_key from public.media_asset where source_id = %s and status <> 'deleted'", (source_id,))]
    for start in range(0, len(keys), 100):
        store.delete(keys[start:start + 100])
    with db.tx() as conn:
        conn.execute("update public.media_asset set status = 'deleted' where source_id = %s", (source_id,))
        conn.execute(
            """update public.transcript_segment t set text = '' from public.extraction_revision e
               where t.extraction_revision_id = e.id and e.source_id = %s""", (source_id,))
        conn.execute(
            """update public.frame_region f set text = '' from public.extraction_revision e
               where f.extraction_revision_id = e.id and e.source_id = %s""", (source_id,))
        conn.execute(
            """update public.upload_session set status = 'cancelled' where source_id = %s and status = 'open'""", (source_id,))


def sweep() -> dict:
    """Thu gom định kỳ: phiên upload hết hạn, video gốc quá thời gian ân hạn, staging của nguồn lỗi (mục 4.2)."""
    settings = get_settings()
    store = BlobStore(settings.media_bucket)
    with db.tx() as conn:
        sessions = conn.execute(
            """update public.upload_session set status = 'expired' where status = 'open' and expires_at < now()
               returning object_prefix, part_count, caption_object_key""").fetchall()
        assets = conn.execute(
            """update public.media_asset a set status = 'deleted' where a.status = 'ready' and (
                   (a.retain_until is not null and a.retain_until < now())
                or (a.kind = 'original_part' and a.created_at < now() - make_interval(secs => %s)
                    and exists (select 1 from public.source s where s.id = a.source_id and s.status = 'failed')))
               returning object_key""", (settings.failed_media_ttl_seconds,)).fetchall()
    keys = [a["object_key"] for a in assets]
    for session in sessions:
        keys += [f"{session['object_prefix']}part-{i:04d}" for i in range(session["part_count"])]
        if session["caption_object_key"]:
            keys.append(session["caption_object_key"])
    for start in range(0, len(keys), 100):
        store.delete(keys[start:start + 100])
    return {"sessions_expired": len(sessions), "assets_deleted": len(assets)}
