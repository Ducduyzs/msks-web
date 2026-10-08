"""Kiểm thử end-to-end trên Supabase thật: đăng nhập → nạp nguồn → hỏi → SSE → evidence → xóa nguồn.

    python scripts/e2e_smoke.py [--pdf path/to/file.pdf] [--keep]

Cần API (cổng 8000) và worker đang chạy. Tạo 2 user tạm qua Supabase Admin API và xóa chúng
(kèm toàn bộ dữ liệu, cascade) khi kết thúc, trừ khi có --keep. Gọi LLM thật (tốn vài trăm token).
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from msks.settings import get_settings  # noqa: E402
from msks.storage import BlobStore  # noqa: E402

API = "http://localhost:8000/api"
DOC_A = """# Phương pháp

RAPTOR recursively embeds, clusters, and summarizes chunks of text, constructing a tree with differing levels of summarization from the bottom up. At inference time, RAPTOR retrieves from this tree.

# Agreement Ranking

Agreement Ranking fuses the cross-encoder ranking with the SBERT query–leaf similarity ranking using reciprocal rank fusion with k = 60. It needs no tree, no summaries and no LLM calls at indexing time.
"""
DOC_B = """# Kết quả trên PeerQA

On PeerQA with gpt-4o-mini, Agreement Ranking was non-inferior to RAPTOR, with a difference of +0.011 evidence F1. Building the index for 70 papers took 3097 seconds for RAPTOR and 32 seconds for Agreement Ranking. Agreement Ranking was not shown to be better than full-document reranking.
"""

passed: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    passed.append((ok, label))
    print(("  OK  " if ok else " FAIL ") + label, flush=True)


class Admin:
    def __init__(self):
        s = get_settings()
        self.base = s.supabase_url.rstrip("/") + "/auth/v1"
        self.secret = {"apikey": s.supabase_secret_key, "Authorization": f"Bearer {s.supabase_secret_key}"}
        self.public = {"apikey": s.supabase_publishable_key or ""}

    def create(self, email: str, password: str) -> str:
        r = httpx.post(f"{self.base}/admin/users", headers=self.secret,
                       json={"email": email, "password": password, "email_confirm": True}, timeout=30)
        r.raise_for_status()
        return r.json()["id"]

    def sign_in(self, email: str, password: str) -> str:
        r = httpx.post(f"{self.base}/token?grant_type=password", headers=self.public,
                       json={"email": email, "password": password}, timeout=30)
        r.raise_for_status()
        return r.json()["access_token"]

    def delete(self, user_id: str) -> None:
        httpx.delete(f"{self.base}/admin/users/{user_id}", headers=self.secret, timeout=30).raise_for_status()


def session(token: str) -> httpx.Client:
    client = httpx.Client(base_url=API, timeout=60)
    r = client.post("/auth/session", json={"access_token": token})
    r.raise_for_status()
    client.headers["X-CSRF-Token"] = client.cookies.get("csrf_token")
    return client


def wait_sources(client: httpx.Client, ws: str, timeout: float = 900) -> list[dict]:
    t0 = time.time()
    while time.time() - t0 < timeout:
        items = client.get(f"/workspaces/{ws}/sources").json()["items"]
        busy = [s for s in items if s["status"] not in ("ready", "failed", "deleted")]
        print(f"    nguồn: {[(s['alias'], s['status']) for s in items]}", flush=True)
        if not busy:
            return items
        time.sleep(5)
    raise TimeoutError("nguồn chưa sẵn sàng")


def stream(client: httpx.Client, run_id: str, last_event_id: int | None = None) -> list[dict]:
    events: list[dict] = []
    headers = {"Last-Event-ID": str(last_event_id)} if last_event_id else {}
    with client.stream("GET", f"/runs/{run_id}/stream", headers=headers, timeout=300) as response:
        for line in response.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                events.append(event)
                if event["type"] == "stage":
                    print(f"    · {event['label']}", flush=True)
                if event["type"] == "done":
                    break
    return events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    admin = Admin()
    tag = secrets.token_hex(4)
    password = secrets.token_urlsafe(18)
    users = []
    workspaces: list[str] = []
    try:
        print("Tài khoản và phiên:")
        u1 = admin.create(f"msks-e2e-{tag}-a@example.com", password)
        users.append(u1)
        u2 = admin.create(f"msks-e2e-{tag}-b@example.com", password)
        users.append(u2)
        c1 = session(admin.sign_in(f"msks-e2e-{tag}-a@example.com", password))
        c2 = session(admin.sign_in(f"msks-e2e-{tag}-b@example.com", password))
        check(c1.get("/auth/me").json()["id"] == u1, "JWT Supabase được xác minh qua JWKS, cookie phiên hoạt động")
        r = httpx.post(f"{API}/workspaces", json={"name": "x"}, cookies={"msks_session": c1.cookies.get("msks_session")})
        check(r.status_code == 403 and r.json()["code"] == "csrf_failed", "ghi bằng cookie thiếu CSRF bị từ chối")
        check(httpx.get(f"{API}/workspaces").status_code == 401, "không đăng nhập → 401")

        print("Nạp nguồn:")
        ws = c1.post("/workspaces", json={"name": f"E2E {tag}"}).json()["id"]
        workspaces.append(ws)
        key = secrets.token_hex(8)
        files = [("phuong-phap.md", DOC_A.encode(), "text/markdown"), ("ket-qua.md", DOC_B.encode(), "text/markdown"),
                 ("ket-qua-ban-sao.md", DOC_B.encode(), "text/markdown")]
        if args.pdf:
            files.append((args.pdf.name, args.pdf.read_bytes(), "application/pdf"))
        first = c1.post(f"/workspaces/{ws}/sources", files={"file": files[0]}, headers={"Idempotency-Key": key})
        check(first.status_code == 202 and first.json()["alias"] == "S1", "upload trả 202, alias S1")
        again = c1.post(f"/workspaces/{ws}/sources", files={"file": files[0]}, headers={"Idempotency-Key": key})
        check(again.json()["id"] == first.json()["id"], "cùng Idempotency-Key không tạo nguồn trùng")
        for item in files[1:]:
            c1.post(f"/workspaces/{ws}/sources", files={"file": item}, headers={"Idempotency-Key": secrets.token_hex(8)})
        bad = c1.post(f"/workspaces/{ws}/sources", files={"file": ("fake.pdf", b"not a pdf", "application/pdf")})
        check(bad.status_code == 415, "file .pdf giả bị từ chối theo nội dung thực (415)")
        sources = wait_sources(c1, ws)
        by_alias = {s["alias"]: s for s in sources}
        check(all(s["status"] == "ready" for s in sources), "mọi nguồn READY, có revision")
        check(by_alias["S3"]["origin_group_id"] == by_alias["S2"]["origin_group_id"] and bool(by_alias["S3"].get("grouping_reason")),
              "bản sao S3 được gom cùng nhóm nguồn gốc với S2")
        check(by_alias["S1"]["revision"]["page_count"] is None, "Markdown không phân trang (page_count = null)")
        if args.pdf:
            pdf = by_alias["S4"]
            content = c1.get(f"/sources/{pdf['id']}/content", params={"revision_id": pdf["revision"]["parse_revision_id"]}).json()
            pages = content["pages"] or []
            print(f"    PDF: {pdf['revision']['page_count']} trang, {pdf['revision']['leaf_count']} leaf, khối trang {[p['page'] for p in pages]}")
            check(pdf["status"] == "ready" and pdf["revision"]["page_count"] == 2, "PDF parse bằng Docling, đúng 2 trang")
            check(bool(pages) and pages[0]["char_start"] == 0 and pages[-1]["char_end"] == len(content["text"])
                  and all(a["char_end"] == b["char_start"] for a, b in zip(pages, pages[1:])),
                  "khối trang phủ liên tục toàn bộ canonical text")

        print("Hỏi đáp:")
        q = {"question": "How long did index building take for RAPTOR and for Agreement Ranking?", "source_ids": [], "budget": 1024, "mode": "standard"}
        accepted = c1.post(f"/workspaces/{ws}/qa", json=q, headers={"Idempotency-Key": "q-" + tag})
        run_id = accepted.json()["run_id"]
        check(accepted.status_code == 202, "QA trả 202 + run_id")
        dup = c1.post(f"/workspaces/{ws}/qa", json=q, headers={"Idempotency-Key": "q-" + tag})
        check(dup.json()["run_id"] == run_id, "QA idempotent theo key")
        conflict = c1.post(f"/workspaces/{ws}/qa", json={**q, "budget": 512}, headers={"Idempotency-Key": "q-" + tag})
        check(conflict.status_code == 409, "cùng key, khác nội dung → 409")
        events = stream(c1, run_id)
        seqs = [e["seq"] for e in events]
        check(seqs == sorted(set(seqs)) and events[-1]["type"] == "done", "SSE có seq tăng dần, kết thúc bằng done")
        replay = stream(c1, run_id, last_event_id=seqs[len(seqs) // 2])
        check([e["seq"] for e in replay] == seqs[len(seqs) // 2 + 1:], "SSE phát lại đúng các event sau Last-Event-ID")
        run = c1.get(f"/runs/{run_id}").json()
        print(f"    status={run['status']} answer={run['answer_status']} claims={len(run['claims'])} "
              f"rejected={len(run['rejected_claims'])} metrics={ {k: run['metrics'][k] for k in ('generated_count', 'accepted_count', 'primary_attribution_risk_accepted')} }")
        for claim in run["claims"]:
            print(f"    - {claim['text']}  [{', '.join(e['source_alias'] for e in claim['evidence'])}]")
        check(run["status"] == "succeeded" and run["answer_status"] == "answered" and run["claims"], "run hoàn tất, có claim đã kiểm chứng")
        streamed = {e["claim"]["id"] for e in events if e["type"] == "claim_verified"}
        check(streamed == {c["id"] for c in run["claims"]}, "SSE chỉ phát claim đã kiểm chứng và đã lưu")
        ok_offsets = True
        for claim in run["claims"]:
            for e in claim["evidence"]:
                content = c1.get(f"/sources/{e['source_id']}/content", params={"revision_id": e["parse_revision_id"]}).json()
                ok_offsets &= content["text"][e["quote_start"]:e["quote_end"]] == e["evidence_snapshot"]
                ok_offsets &= bool(e["context_block_id"])
        check(ok_offsets, "offset code point của evidence khớp canonical text của đúng revision")
        check(run["provenance"]["source_snapshot"] and run["provenance"]["calibration_artifact"] is None,
              "provenance có snapshot nguồn; không có calibration → kết quả thử nghiệm")
        md = c1.get(f"/runs/{run_id}/export", params={"format": "md"})
        check(md.status_code == 200 and run["claims"][0]["text"] in md.text, "xuất Markdown từ dữ liệu đã lưu")
        check(c1.get(f"/runs/{run_id}/export", params={"format": "pdf"}).status_code == 501, "xuất PDF trả 501 (G4)")
        runs = c1.get(f"/workspaces/{ws}/runs").json()
        check(runs["items"][0]["run_id"] == run_id, "lịch sử liệt kê run")

        print("So sánh thiếu một phía:")
        only = c1.post(f"/workspaces/{ws}/qa", json={"question": "Compare RAPTOR versus Agreement Ranking", "source_ids": [by_alias["S2"]["id"], by_alias["S3"]["id"]],
                                                     "budget": 1024, "mode": "standard"}).json()["run_id"]
        stream(c1, only)
        check(c1.get(f"/runs/{only}").json()["answer_status"] == "insufficient_evidence", "câu so sánh trên một nhóm nguồn → insufficient_evidence")

        print("Cách ly và xóa:")
        check(c2.get(f"/runs/{run_id}").status_code == 404, "user khác không đọc được run (404)")
        check(c2.get(f"/workspaces/{ws}/sources").status_code == 404, "user khác không liệt kê được nguồn")
        target = by_alias["S2"]
        deleted = c1.delete(f"/sources/{target['id']}")
        check(deleted.status_code == 202, "xóa nguồn trả 202 + job")
        gone = c1.get(f"/sources/{target['id']}/content", params={"revision_id": target["revision"]["parse_revision_id"]})
        check(gone.status_code == 410, "nguồn đã tombstone → nội dung 410")
        masked = c1.get(f"/runs/{run_id}").json()
        hidden = [e for c in masked["claims"] for e in c["evidence"] if e["source_id"] == target["id"]]
        check(all(e.get("source_deleted") and e["evidence_snapshot"] == "" for e in hidden), "run cũ giữ tham chiếu nhưng ẩn nội dung nguồn đã xóa")
        for _ in range(60):
            job = c1.get(f"/jobs/{deleted.json()['job_id']}").json()
            if job["state"] in ("succeeded", "failed"):
                break
            time.sleep(2)
        check(job["state"] == "succeeded", "job xóa (GC vector + file) hoàn tất")
    finally:
        if not args.keep:
            store = BlobStore()
            for ws_id in workspaces:  # file gốc nằm ngoài Postgres nên không bị cascade
                store.delete(store.list_keys(ws_id))
            for user in users:
                admin.delete(user)  # cascade: workspace → nguồn, run, claim...
            print(f"Đã xóa {len(users)} user tạm và dữ liệu của họ.")
    failed = [label for ok, label in passed if not ok]
    print(f"\n{len(passed) - len(failed)}/{len(passed)} kiểm tra đạt.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
