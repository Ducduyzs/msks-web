"""Bài giảng tiếng Việt end-to-end: ASR của pipeline (WER, số, timestamp) + hỏi đáp tiếng Việt có kiểm chứng.

    python scripts/make_sample_lecture_vi.py OUT
    python scripts/e2e_lecture_vi.py OUT [--out analysis/lecture_vi_e2e]

Cần API + worker đang chạy (cùng config). Tải bản sạch và bản có nhiễu, đo bản chép do **chính pipeline** tạo
so với kịch bản đọc; hỏi 4 câu tiếng Việt (số liệu, so sánh "không kém", câu không có trong bài). Tạo user tạm
và xóa toàn bộ dữ liệu khi kết thúc. Gọi LLM thật.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
from e2e_lecture import wait_status  # noqa: E402
from e2e_smoke import Admin, session, stream  # noqa: E402
from msks.eval_asr import score  # noqa: E402
from msks.media.asr import STOCK_PHRASES, _plain  # noqa: E402
from msks.settings import get_settings  # noqa: E402
from msks.storage import BlobStore  # noqa: E402

QUESTIONS = [
    ("constant", "Hằng số k trong Agreement Ranking bằng bao nhiêu?"),
    ("numbers", "Lập chỉ mục 70 bài báo mất bao lâu với Agreement Ranking và với RAPTOR?"),
    ("comparison", "Agreement Ranking có tốt hơn RAPTOR trên PeerQA không?"),
    ("absent", "Agreement Ranking được công bố vào năm nào?"),
]

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((ok, label))
    print(("  OK  " if ok else " FAIL ") + label, flush=True)


def upload(client: httpx.Client, ws: str, path: Path) -> dict:
    data = path.read_bytes()
    sess = client.post(f"/workspaces/{ws}/media-upload-sessions",
                       json={"filename": path.name, "size_bytes": len(data), "mime": "video/mp4", "language": "vi"}).json()
    offset = 0
    for part in sess["parts"]:
        httpx.put(part["upload_url"], content=data[offset:offset + part["size"]],
                  headers={"Content-Type": "application/octet-stream"}).raise_for_status()
        offset += part["size"]
    return client.post(f"/media-upload-sessions/{sess['id']}/complete").json()


def transcript_quality(timeline: dict, cues: list[dict]) -> dict:
    hyp = " ".join(s["text"] for s in timeline["segments"])
    ref = " ".join(c["text"] for c in cues)
    total = score(ref, hyp)
    offsets = []
    for cue in cues:
        start, end = cue["start"] * 1000, cue["end"] * 1000
        overlapping = [s for s in timeline["segments"] if s["start_ms"] < end and s["end_ms"] > start]
        if overlapping:
            offsets.append(abs(min(s["start_ms"] for s in overlapping) - start))
    flagged = sum(1 for s in timeline["segments"] if s["flags"])
    return {
        "wer": round(total.wer, 4), "cer": round(total.cer, 4),
        "number_recall": round(total.number_recall, 3) if total.number_recall is not None else None,
        "numbers_in_reference": total.numbers_ref,
        "start_offset_ms_p50": round(statistics.median(offsets)) if offsets else None,
        "start_offset_ms_max": max(offsets) if offsets else None,
        "cues_covered": f"{len(offsets)}/{len(cues)}",
        "segments": len(timeline["segments"]), "segments_flagged": flagged,
        "hypothesis": hyp, "reference": ref,
        "board_regions": [r["text"] for r in timeline["regions"] if r["included"]],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--out", type=Path, default=ROOT / "analysis/lecture_vi_e2e")
    parser.add_argument("--glossary", help="thuật ngữ cách nhau bằng dấu phẩy, đặt cho workspace trước khi tải video")
    args = parser.parse_args()
    cues = json.loads((args.folder / "sample_lecture_vi.json").read_text(encoding="utf-8"))["cues"]
    admin, tag, password = Admin(), secrets.token_hex(4), secrets.token_urlsafe(18)
    users, workspaces, report = [], [], {"asr": {}, "qa": {}}
    try:
        users.append(admin.create(f"msks-vi-{tag}@example.com", password))
        client = session(admin.sign_in(f"msks-vi-{tag}@example.com", password))
        ws = client.post("/workspaces", json={"name": f"Bài giảng VI {tag}"}).json()["id"]
        workspaces.append(ws)
        if args.glossary:
            too_long = client.put(f"/workspaces/{ws}/asr-glossary", json={"terms": [f"t{i}" for i in range(41)]})
            check(too_long.status_code == 422 and too_long.json()["code"] == "invalid_glossary", "glossary vượt giới hạn → 422")
            terms = client.put(f"/workspaces/{ws}/asr-glossary",
                               json={"terms": [t for t in args.glossary.split(",")]}).json()["terms"]
            check(client.get(f"/workspaces/{ws}").json()["asr_glossary"] == terms, f"workspace lưu glossary ({len(terms)} thuật ngữ)")
            report["glossary"] = terms

        print("ASR của pipeline:")
        sources = {}
        for variant, name in (("clean", "sample_lecture_vi.mp4"), ("noisy", "sample_lecture_vi_noisy.mp4")):
            t0 = time.time()
            source = wait_status(client, ws, upload(client, ws, args.folder / name)["id"])
            sources[variant] = source
            timeline = client.get(f"/sources/{source['id']}/timeline",
                                  params={"revision_id": source["revision"]["parse_revision_id"]}).json()
            quality = transcript_quality(timeline, cues)
            asr_versions = (timeline.get("versions") or {}).get("asr") or {}
            quality.update({"status": source["status"], "seconds": round(time.time() - t0),
                            "asr_model": asr_versions.get("model"), "glossary_sha256": asr_versions.get("glossary_sha256")})
            if args.glossary:
                check(bool(asr_versions.get("glossary_sha256")), f"[{variant}] extraction ghi glossary_sha256")
            report["asr"][variant] = quality
            print(f"    [{variant}] WER={quality['wer']} CER={quality['cer']} số={quality['number_recall']} "
                  f"lệch đầu câu p50={quality['start_offset_ms_p50']} ms max={quality['start_offset_ms_max']} ms "
                  f"({quality['seconds']} s, {quality['status']})")
            print(f"      HYP: {quality['hypothesis'][:300]}")
            check(source["status"] in ("ready", "ready_limited"), f"[{variant}] bài giảng tiếng Việt xử lý xong")
            check(not STOCK_PHRASES.search(_plain(quality["hypothesis"])),
                  f"[{variant}] không có câu bịa kiểu 'hẹn gặp lại/cảm ơn đã theo dõi' ở đuôi im lặng")
        check(report["asr"]["clean"]["wer"] < 0.25, f"[clean] WER < 25% ({report['asr']['clean']['wer']})")
        check(any("Chưa chứng minh" in r for r in report["asr"]["clean"]["board_regions"]), "OCR đọc dòng bảng viết thêm (tiếng Việt)")

        print("Hỏi đáp tiếng Việt (nguồn bản sạch):")
        for key, question in QUESTIONS:
            run_id = client.post(f"/workspaces/{ws}/qa", json={"question": question, "source_ids": [sources["clean"]["id"]],
                                                               "budget": 1024, "mode": "standard"}).json()["run_id"]
            stream(client, run_id)
            run = client.get(f"/runs/{run_id}").json()
            claims = [{"text": c["text"], "modality": c["evidence"][0]["modality"],
                       "locator_ms": [(c["evidence"][0].get("locator") or {}).get("start_ms"),
                                      (c["evidence"][0].get("locator") or {}).get("end_ms")],
                       "basis": c["support_basis"], "evidence": c["evidence"][0]["evidence_snapshot"][:160]}
                      for c in run["claims"]]
            rejected = [{"text": r["text"], "reason": r["reason"]} for r in run["rejected_claims"]]
            report["qa"][key] = {"question": question, "profile": run["profile"], "answer_status": run["answer_status"],
                                 "claims": claims, "rejected": rejected}
            print(f"    [{key}] {question} → {run['answer_status']} ({run['profile']})")
            for c in claims:
                print(f"      ✓ {c['text']}  [{c['modality']} {c['locator_ms']} ms, {c['basis']}]")
            for r in rejected:
                print(f"      ✗ {r['text']}  ({r['reason']})")
        qa = report["qa"]
        check(qa["constant"]["answer_status"] == "answered" and any("60" in c["text"] for c in qa["constant"]["claims"]),
              "trả lời đúng hằng số k = 60")
        check(any("32" in c["text"] for c in qa["numbers"]["claims"]), "giữ đúng số liệu 32 giây")
        overclaim = [c for c in qa["comparison"]["claims"]
                     if "tốt hơn" in c["text"] and not any(w in c["text"] for w in ("không", "chưa"))]
        check(not overclaim, "không publish claim 'tốt hơn' khi nguồn chỉ nói 'không kém'")
        check(qa["absent"]["answer_status"] == "insufficient_evidence" or not qa["absent"]["claims"],
              "câu hỏi không có trong bài → không bịa câu trả lời")
        vietnamese = re.compile(r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]", re.IGNORECASE)
        check(all(vietnamese.search(c["text"]) for item in qa.values() for c in item["claims"]),
              "claim viết bằng tiếng Việt như câu hỏi")
        check(all(c["basis"] == "automatic_transcript" or c["modality"] == "board"
                  for item in qa.values() for c in item["claims"]), "claim từ lời giảng mang nhãn bản chép tự động")
    finally:
        for bucket in (get_settings().storage_bucket, get_settings().media_bucket):
            store = BlobStore(bucket)
            for ws_id in workspaces:
                keys = store.list_keys(ws_id)
                for start in range(0, len(keys), 100):
                    store.delete(keys[start:start + 100])
        for user in users:
            admin.delete(user)
        print("Đã xóa user tạm, dữ liệu và file.")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} kiểm tra đạt. → {args.out.with_suffix('.json')}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
