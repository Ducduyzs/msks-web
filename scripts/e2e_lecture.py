"""End-to-end bài giảng trên Supabase + GPU + OpenAI thật (LECTURE_ARCHITECTURE.md L0–L2).

    python scripts/make_sample_lecture.py OUT
    python scripts/e2e_lecture.py OUT/sample_lecture.mp4 OUT/sample_lecture.srt

Cần API + worker đang chạy với CÙNG cấu hình (config_hash). Để thử upload nhiều phần với video nhỏ, chạy cả hai
với MSKS_MEDIA_PART_BYTES nhỏ (vd. 150000). Tạo 2 user tạm và xóa toàn bộ (DB + Storage) khi kết thúc.
"""
from __future__ import annotations

import argparse
import secrets
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
from e2e_smoke import Admin, session, stream  # noqa: E402
from msks.settings import get_settings  # noqa: E402
from msks.storage import BlobStore  # noqa: E402

DOC = """# Ghi chú

Agreement Ranking was evaluated on PeerQA with gpt-4o-mini. RAPTOR builds a tree of summaries before retrieval.
"""

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((ok, label))
    print(("  OK  " if ok else " FAIL ") + label, flush=True)


def wait_status(client: httpx.Client, ws: str, source_id: str, done=("ready", "ready_limited", "review_required", "failed"),
                timeout: float = 1200) -> dict:
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        items = {s["id"]: s for s in client.get(f"/workspaces/{ws}/sources").json()["items"]}
        source = items.get(source_id)
        if source and source["status"] != last:
            last = source["status"]
            print(f"    {source['alias']}: {last} ({source['progress']:.2f})", flush=True)
        if source and source["status"] in done:
            return source
        time.sleep(3)
    raise TimeoutError(source_id)


def wait_job(client: httpx.Client, job_id: str, timeout: float = 900) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        job = client.get(f"/jobs/{job_id}").json()
        if job["state"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(3)
    raise TimeoutError(job_id)


def ask(client: httpx.Client, ws: str, question: str, source_ids: list[str] | None = None) -> httpx.Response:
    return client.post(f"/workspaces/{ws}/qa", json={"question": question, "source_ids": source_ids or [],
                                                      "budget": 1024, "mode": "standard"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("srt", type=Path)
    args = parser.parse_args()
    admin, tag, password = Admin(), secrets.token_hex(4), secrets.token_urlsafe(18)
    users, workspaces = [], []
    try:
        users.append(admin.create(f"msks-lecture-{tag}-a@example.com", password))
        users.append(admin.create(f"msks-lecture-{tag}-b@example.com", password))
        c1 = session(admin.sign_in(f"msks-lecture-{tag}-a@example.com", password))
        c2 = session(admin.sign_in(f"msks-lecture-{tag}-b@example.com", password))
        ws = c1.post("/workspaces", json={"name": f"Lecture {tag}"}).json()["id"]
        workspaces.append(ws)

        print("Phiên upload nhiều phần:")
        data = args.video.read_bytes()
        bad = c1.post(f"/workspaces/{ws}/media-upload-sessions", json={"filename": "x.exe", "size_bytes": 10, "mime": "video/mp4"})
        check(bad.status_code == 415, "từ chối phần mở rộng không phải video (415)")
        big = c1.post(f"/workspaces/{ws}/media-upload-sessions",
                      json={"filename": "big.mp4", "size_bytes": 10**10, "mime": "video/mp4"})
        check(big.status_code == 413, "từ chối video vượt giới hạn (413)")
        key = secrets.token_hex(8)
        body = {"filename": args.video.name, "size_bytes": len(data), "mime": "video/mp4", "language": "en"}
        created = c1.post(f"/workspaces/{ws}/media-upload-sessions", json=body, headers={"Idempotency-Key": key})
        sess = created.json()
        check(created.status_code == 201 and sess["part_count"] >= 1, f"tạo phiên: {sess['part_count']} phần × {sess['part_bytes']} B")
        again = c1.post(f"/workspaces/{ws}/media-upload-sessions", json=body, headers={"Idempotency-Key": key}).json()
        check(again["id"] == sess["id"], "cùng Idempotency-Key trả lại cùng phiên")
        check(c2.get(f"/media-upload-sessions/{sess['id']}").status_code == 404, "user khác không xem được phiên upload")
        offset = 0
        chunks = []
        for part in sess["parts"]:
            chunks.append(data[offset:offset + part["size"]])
            offset += part["size"]
        first = sess["parts"][0]
        httpx.put(first["upload_url"], content=chunks[0], headers={"Content-Type": "application/octet-stream"}).raise_for_status()
        if sess["part_count"] > 1:
            early = c1.post(f"/media-upload-sessions/{sess['id']}/complete")
            check(early.status_code == 409 and early.json()["code"] == "upload_incomplete", "complete khi thiếu phần → 409")
        resumed = c1.get(f"/media-upload-sessions/{sess['id']}").json()
        check(resumed["parts"][0]["uploaded"] and all("upload_url" in p for p in resumed["parts"][1:]),
              "resume: phần đã tải được ghi nhận, phần còn thiếu có URL mới")
        for part in resumed["parts"][1:]:
            httpx.put(part["upload_url"], content=chunks[part["index"]],
                      headers={"Content-Type": "application/octet-stream"}).raise_for_status()
        cap = c1.post(f"/media-upload-sessions/{sess['id']}/caption", files={"file": ("bad.srt", b"no cues", "text/plain")})
        check(cap.status_code == 422, "phụ đề không có cue hợp lệ bị từ chối")
        done = c1.post(f"/media-upload-sessions/{sess['id']}/complete")
        video = done.json()
        check(done.status_code == 202 and video["kind"] == "video", f"complete → nguồn {video['alias']} (video)")
        check(c1.post(f"/media-upload-sessions/{sess['id']}/complete").json()["id"] == video["id"], "complete idempotent")

        print("Pipeline media (ASR + frame + OCR):")
        t0 = time.time()
        video = wait_status(c1, ws, video["id"])
        print(f"    xử lý {time.time() - t0:.0f} s; coverage={video.get('coverage')}")
        check(video["status"] in ("ready", "ready_limited"), f"bài giảng xử lý xong ({video['status']})")
        check(bool(video.get("capabilities", {}).get("audio_available")) and bool(video["capabilities"].get("visual_available")),
              "capabilities: có âm thanh và hình")
        timeline = c1.get(f"/sources/{video['id']}/timeline", params={"revision_id": video["revision"]["parse_revision_id"]}).json()
        segs, regs = timeline["segments"], timeline["regions"]
        print(f"    {len(segs)} segment, {len(regs)} vùng chữ; ví dụ: {segs[0]['text'][:70]!r} | {[r['text'] for r in regs][:4]}")
        check(len(segs) >= 2 and all(s["origin"] == "asr" for s in segs), "transcript ASR có mốc thời gian")
        check(any("3097" in r["text"] for r in regs), "OCR đọc được số liệu trên slide")
        check(any("Chưa chứng minh" in r["text"] for r in regs), "bảng viết dần: dòng thêm sau vẫn được đọc")
        check(any("Không" in r["text"] or "chỉ mục" in r["text"] for r in regs), "OCR giữ dấu tiếng Việt")
        audio = next(a for a in timeline["assets"] if a["kind"] == "audio")
        frame = next(a for a in timeline["assets"] if a["kind"] == "frame")
        for asset in (audio, frame):
            signed = c1.get(f"/sources/{video['id']}/media/{asset['id']}").json()
            fetched = httpx.get(signed["url"])
            check(fetched.status_code == 200 and len(fetched.content) > 1000, f"URL hạn ngắn đọc được {asset['kind']}")
        check(c2.get(f"/sources/{video['id']}/media/{audio['id']}").status_code == 404, "user khác không lấy được URL media")

        print("Phạm vi trộn tài liệu + bài giảng:")
        doc = c1.post(f"/workspaces/{ws}/sources", files={"file": ("ghi-chu.md", DOC.encode(), "text/markdown")}).json()
        wait_status(c1, ws, doc["id"])
        mixed = ask(c1, ws, "How long did building the index take for Agreement Ranking?")
        check(mixed.status_code == 422 and mixed.json()["code"] == "profile_index_missing",
              "không trộn profile: tài liệu chưa có index lecture_vi_v1 → 422")
        job = c1.post(f"/sources/{doc['id']}/reindex", json={"profile": "lecture_vi_v1"}).json()
        check(wait_job(c1, job["id"])["state"] == "succeeded", "reindex tài liệu sang profile lecture_vi_v1")

        print("Hỏi đáp trên bài giảng:")
        run_id = ask(c1, ws, "How long did building the index take for Agreement Ranking and for RAPTOR?").json()["run_id"]
        stream(c1, run_id)
        run = c1.get(f"/runs/{run_id}").json()
        print(f"    profile={run['profile']} answer={run['answer_status']} claims={len(run['claims'])} rejected={run['rejected_claims']}")
        for claim in run["claims"]:
            e = claim["evidence"][0]
            loc = e.get("locator") or {}
            print(f"    - {claim['text']} [{e['source_alias']} {e['modality']} {loc.get('start_ms')}–{loc.get('end_ms')} ms, "
                  f"{e['transcription_state']}, basis={claim['support_basis']}]")
        check(run["profile"] == "lecture_vi_v1" and run["answer_status"] == "answered", "QA dùng profile bài giảng và có claim")
        lecture_claims = [c for c in run["claims"] if c["evidence"][0]["modality"] in ("speech", "board", "caption")]
        check(bool(lecture_claims), "có claim dẫn từ bài giảng")
        ok_locator = all(c["evidence"][0]["locator"] and c["evidence"][0]["locator"]["items"]
                         and c["support_basis"] == "automatic_transcript" for c in lecture_claims)
        check(ok_locator, "evidence bài giảng có locator thời gian + nhãn 'bản chép tự động'")
        content = c1.get(f"/sources/{video['id']}/content", params={"revision_id": video["revision"]["parse_revision_id"]}).json()
        check(all(content["text"][e["quote_start"]:e["quote_end"]] == e["evidence_snapshot"]
                  for c in lecture_claims for e in c["evidence"]), "quote khớp canonical text theo code point")

        print("YouTube (phụ đề do người dùng cung cấp) và Drive:")
        no_cap = c1.post(f"/workspaces/{ws}/sources/youtube", data={"url": "https://youtu.be/dQw4w9WgXcQ"})
        check(no_cap.status_code == 501 and no_cap.json()["code"] == "youtube_oauth_not_configured",
              "YouTube không có phụ đề → 501, không tự tải video")
        yt = c1.post(f"/workspaces/{ws}/sources/youtube",
                     data={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "language": "en", "title": "Bài giảng YouTube"},
                     files={"caption": (args.srt.name, args.srt.read_bytes(), "application/x-subrip")}).json()
        yt = wait_status(c1, ws, yt["id"])
        check(yt["status"] == "ready_limited" and yt["capabilities"]["visual_available"] is False,
              "nguồn chỉ có phụ đề → READY_LIMITED, không nhận là đã đọc hình")
        drive = c1.post(f"/workspaces/{ws}/sources/drive")
        check(drive.status_code == 501 and drive.json()["code"] == "drive_oauth_not_configured", "Drive chưa cấu hình OAuth → 501")
        yt_run = ask(c1, ws, "What constant k does Agreement Ranking use?", [yt["id"]]).json()["run_id"]
        stream(c1, yt_run)
        yt_result = c1.get(f"/runs/{yt_run}").json()
        check(any(e["modality"] == "caption" for c in yt_result["claims"] for e in c["evidence"]),
              f"trả lời từ phụ đề YouTube ({yt_result['answer_status']})")

        print("Sửa transcript → revision mới:")
        old_revision = video["revision"]["parse_revision_id"]
        target = segs[0]
        edit = c1.post(f"/sources/{video['id']}/transcript-revisions",
                       json={"base_revision_id": old_revision, "segments": [{"id": target["id"], "text": target["text"] + " (đã sửa)"}]})
        check(edit.status_code == 202, "gửi bản sửa tạo job reindex")
        check(wait_job(c1, edit.json()["id"])["state"] == "succeeded", "job sửa transcript hoàn tất")
        video2 = next(s for s in c1.get(f"/workspaces/{ws}/sources").json()["items"] if s["id"] == video["id"])
        new_revision = video2["revision"]["parse_revision_id"]
        timeline2 = c1.get(f"/sources/{video['id']}/timeline", params={"revision_id": new_revision}).json()
        edited = next(s for s in timeline2["segments"] if s["text"].endswith("(đã sửa)"))
        check(new_revision != old_revision and edited["origin"] == "manual_edit" and edited["reviewed"],
              "revision mới: đoạn sửa là manual_edit, đã đối chiếu")
        check(c1.get(f"/sources/{video['id']}/timeline", params={"revision_id": old_revision}).status_code == 200
              and c1.get(f"/runs/{run_id}").json()["claims"][0]["evidence"][0]["parse_revision_id"] in (old_revision, doc_revision(c1, ws, doc)),
              "run cũ vẫn trỏ revision cũ (không ghi đè lịch sử)")

        print("Xóa nguồn bài giảng:")
        job_id = c1.delete(f"/sources/{video['id']}").json()["job_id"]
        check(c1.get(f"/sources/{video['id']}/media/{audio['id']}").status_code == 410, "nguồn tombstone → media 410")
        check(wait_job(c1, job_id)["state"] == "succeeded", "job xóa hoàn tất")
        remaining = BlobStore(get_settings().media_bucket).list_keys(f"{ws}/{video['id']}")
        check(not remaining, "đã xóa audio/frame trong Storage")
    finally:
        for bucket in (get_settings().storage_bucket, get_settings().media_bucket):
            store = BlobStore(bucket)
            for ws_id in workspaces:
                keys = store.list_keys(ws_id)
                for start in range(0, len(keys), 100):
                    store.delete(keys[start:start + 100])
        for user in users:
            admin.delete(user)
        print(f"Đã xóa {len(users)} user tạm, dữ liệu và file của họ.")
    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} kiểm tra đạt.")
    if failed:
        raise SystemExit(1)


def doc_revision(client: httpx.Client, ws: str, doc: dict) -> str:
    items = {s["id"]: s for s in client.get(f"/workspaces/{ws}/sources").json()["items"]}
    return items[doc["id"]]["revision"]["parse_revision_id"]


if __name__ == "__main__":
    main()
