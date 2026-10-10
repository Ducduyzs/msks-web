# MSKS — Tổng hợp kiến thức đa nguồn có kiểm chứng

**Mới tiếp nhận dự án? Đọc [HANDOFF.md](HANDOFF.md) trước.** · Kiến trúc: [ARCHITECTURE.md](ARCHITECTURE.md) · Frontend: [web/README.md](web/README.md) · Database: [db/README.md](db/README.md)

```text
trình duyệt ──► Vite (5173, proxy /api) ──► FastAPI (8000) ──► Supabase Postgres + pgvector + Storage
                     │                            ▲
                     └── Supabase Auth (đăng nhập)│ job / run_event
                                                  └── worker (GPU: BGE-M3, reranker, SBERT, NLI) ──► OpenAI
```

## Chạy

Yêu cầu: Python 3.10+ có torch CUDA (worker), Node 22, dự án Supabase đã chạy migration.

```bash
# 1. Môi trường Python (dùng chung torch/FlagEmbedding đã cài trong Python hệ thống)
python -m venv --system-site-packages .venv
.venv/Scripts/pip install -e . --no-deps
.venv/Scripts/pip install "psycopg[binary,pool]>=3.2"     # nếu chưa có; ML: pip install -e ".[ml]"

# 2. Database
.venv/Scripts/python db/migrate.py up

# 3. Ba process (mỗi lệnh một terminal)
.venv/Scripts/python -m uvicorn msks.api:app --port 8000
.venv/Scripts/python -m msks.worker
cd web && npm install && npm run dev                       # http://localhost:5173
```

Cấu hình:

- **Backend** đọc `.env` ở gốc: chuỗi kết nối Postgres, `SUPABASE_SECRET_KEY`, JWKS và tùy chọn `MSKS_*` (xem [.env.example](.env.example)). `OPENAI_API_KEY` đọc từ biến môi trường.
- **Frontend** Vite đọc `web/.env` và `web/.env.local` (file local ưu tiên hơn), chứa `VITE_API_MODE`, URL Supabase và khóa **publishable**. Không đặt bí mật nào trong `web/`: biến `VITE_*` sẽ nằm trong bundle gửi tới trình duyệt. Backend và script migration chỉ đọc `.env` gốc/biến môi trường.

## Kiểm thử

| Lệnh | Phạm vi |
|---|---|
| `.venv/Scripts/python -m pytest -q` | Unit test backend (offset code point, phân trang, fingerprint, cursor, trạng thái đáp án) |
| `.venv/Scripts/python db/verify_schema.py` | Ràng buộc + RLS trên database thật, luôn rollback |
| `.venv/Scripts/python scripts/e2e_smoke.py [--pdf file.pdf]` | End-to-end trên Supabase thật với 2 user tạm; tự dọn dữ liệu. Gọi OpenAI (vài trăm token) |
| `cd web && npm test` | Frontend trên API mock (logic + giao diện jsdom) |
| `.venv/Scripts/python scripts/make_sample_lecture.py OUT` rồi `scripts/e2e_lecture.py OUT/sample_lecture.mp4 OUT/sample_lecture.srt` | Bài giảng end-to-end (upload nhiều phần, ASR, OCR, citation thời gian, YouTube caption, sửa transcript, xóa) |

## Mã nguồn backend

| Module | Vai trò |
|---|---|
| `src/msks/api.py` | FastAPI: phiên (cookie HttpOnly + CSRF), workspace, nguồn (202 + Idempotency-Key), QA, run, SSE (`Last-Event-ID`, heartbeat), export |
| `src/msks/auth.py` | Kiểm JWT Supabase qua JWKS (ES256), dependency `current_user` |
| `src/msks/worker.py` | Nạp nguồn (parse → cây → fingerprint → embed → publish generation), xóa (GC), chạy QA |
| `src/msks/jobs.py` | Hàng đợi trên PostgreSQL: `SKIP LOCKED`, lease, heartbeat, `attempt_version`, retry có backoff |
| `src/msks/qa.py` | Pipeline QA profile `product` (mục 7.1): Agreement Ranking, đóng gói, LLM, `verify_visible`, chỉ số 8.4 |
| `src/msks/ingest.py` | Docling/Markdown/TXT → canonical text, offset code point, khối trang, MinHash |
| `src/msks/ml.py` | Model v11 nạp lười trên GPU, khóa concurrency = 1 |
| `src/msks/dto.py` | JSON đúng hợp đồng `web/src/api/types.ts`; che nội dung nguồn đã xóa |
| `src/msks/profiles.py` | Profile model `product` / `lecture_vi_v1`; không trộn embedding giữa profile |
| `src/msks/indexing.py` | Ghi cây node, embed theo profile, publish generation (dùng chung tài liệu/bài giảng/reindex) |
| `src/msks/api_media.py` | Bài giảng: phiên upload nhiều phần, caption, YouTube (caption người dùng), timeline, URL media, sửa transcript, reindex |
| `src/msks/media/` | Probe/FFmpeg, caption SRT/VTT, chọn frame, ASR faster-whisper, OCR EasyOCR, alignment + canonical + source map, pipeline worker |
| `src/edahr/` | Snapshot EDAHR v11 không sửa — [src/EDAHR_VENDOR.md](src/EDAHR_VENDOR.md) |

## Quyết định triển khai MVP (ARCHITECTURE.md mục 0.1)

| Tài liệu | Triển khai MVP | Lý do / đường quay lại |
|---|---|---|
| Qdrant cho dense/sparse | **pgvector** trên Supabase (`leaf_embedding`: dense 1024, sparsevec BGE-M3, SBERT 768), quét chính xác theo generation | Giảm dependency vận hành; phải benchmark trước khi kết luận đạt độ trễ ở 20.000 leaf |
| Celery + Redis, outbox relay | **Hàng đợi trên bảng `job`** (`SKIP LOCKED`, lease, heartbeat, attempt version) | Cùng lý do; PostgreSQL vẫn là nguồn trạng thái phục hồi. Bảng `outbox_event` đã có sẵn khi cần relay sang Celery |
| Local BlobStore | **Supabase Storage**, bucket riêng tư `documents` | Đã có sẵn trong dự án Supabase |
| Auth chưa mô tả | **Supabase Auth** → `POST /api/auth/session` đổi access token lấy cookie HttpOnly + `csrf_token` | Giữ đúng hợp đồng cookie cùng origin + CSRF của mục 10 |
| — | Thêm `GET /api/workspaces`, `GET /workspaces/{id}/runs`, `/api/auth/*`, trường `rerun_of` | Frontend cần |

## Giới hạn hiện tại

Chi tiết lỗi đã sửa, migration chưa áp dụng và việc còn lại: [REVIEW_2026-10-08.md](REVIEW_2026-10-08.md). Các test local không thay thế kiểm thử Supabase/GPU thật.

- **Bài giảng video:** backend L0–L1 + YouTube với phụ đề người dùng cung cấp đã có; Drive và giao diện frontend chưa có. Trạng thái và phát hiện khi đo: [LECTURE_ARCHITECTURE.md mục 13](LECTURE_ARCHITECTURE.md#13-trạng-thái-triển-khai-09102026).
- **Chưa có:** đối chứng chéo, tổng hợp, URL/DOI, DOCX/HTML, OCR cho PDF scan, và xuất DOCX/PDF. Đây là các giai đoạn G3–G4; API trả `501 feature_disabled`.
- **Ngưỡng NLI chưa được hiệu chỉnh:** đang dùng ngưỡng lịch sử của v11 (0,25 / 0,50). Mọi run vì thế mang nhãn "Kết quả thử nghiệm" (`calibration_artifact = null`).
- **Thiếu cache:** chưa có cache cho reranker/NLI/LLM (mục 12.3).
- **Chưa đo hiệu năng:** chưa có số đo N1/N2 (mục 3.2).
