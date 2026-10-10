# MSKS Database (Supabase Postgres)

Schema theo [ARCHITECTURE.md](../ARCHITECTURE.md) mục 5.2, áp dụng bằng SQL migration có checksum.

```bash
python -m venv .venv
.venv/Scripts/pip install -r db/requirements.txt
.venv/Scripts/python db/migrate.py status   # đã/chưa áp dụng
.venv/Scripts/python db/migrate.py up       # áp dụng phần còn thiếu
.venv/Scripts/python db/verify_schema.py    # kiểm tra ràng buộc + RLS, luôn rollback
```

Kết nối chỉ đọc từ `.env` ở thư mục gốc hoặc biến môi trường: dùng `DIRECT_URL` (session pooler, cổng 5432) cho migration. Không đặt cấu hình DB trong `web/`.
Backend chạy request nên dùng `DATABASE_URL` (transaction pooler, cổng 6543); với psycopg qua pgbouncer transaction mode, tắt prepared statement (`prepare_threshold=None`).

## Migration

| File | Nội dung |
|---|---|
| `0001_core_schema.sql` | 17 bảng: workspace, source, source_relation, document/parse revision, index_generation, node, node_edge, run, context_block, claim, claim_evidence, report_section, job, outbox_event, run_event, job_event; hàm `append_run_event`, `next_source_alias` |
| `0002_rls.sql` | RLS cho mọi bảng; `anon` không có quyền; `authenticated` chỉ SELECT dữ liệu workspace mình sở hữu; nội dung nguồn đã tombstone bị ẩn |
| `0003_storage.sql` | Bucket riêng tư `documents` (50 MB/file) làm BlobStore |
| `0004_vector_index.sql` | pgvector dense/sparse/SBERT, canonical text và page map |
| `0005_node_fk_no_action.sql` | Điều chỉnh FK node |
| `0006_tombstone_read_protection.sql` | Chặn raw run_event qua Data API, ẩn canonical text khi source tombstone |
| `0007_lecture_media.sql` | Bài giảng: `upload_session`, `acquisition`, `media_asset`, `extraction_revision`, `transcript_segment`, `frame_region`, `alignment_edge`, `source_map_span`; `node.modality/start_ms/end_ms`; `index_generation.model_profile` (active theo từng profile); `claim_evidence.locator_json/modality/transcription_state`; `job.payload_json`; trạng thái `ready_limited`/`review_required`; bucket riêng tư `media` |
| `0008_media_fk_deferred.sql` | Khóa ngoại media/revision kiểm lúc commit — sửa lỗi xóa cascade cả workspace thất bại (tái hiện bằng `verify_schema.py`) |

Không sửa migration đã áp dụng — tạo file mới. `migrate.py` từ chối khi checksum lệch. Ngày 09/10 đã áp dụng `0006`–`0008` lên dự án Supabase dev (lúc đó 0 workspace); `verify_schema.py` đạt 38/38. Nội dung transcript/OCR/source map/object key media **không** cấp SELECT cho `authenticated` — chỉ đọc qua FastAPI.

## Mô hình phân quyền

- **Ghi**: chỉ backend (kết nối Postgres trực tiếp hoặc `SUPABASE_SECRET_KEY`, bỏ qua RLS). Client không INSERT/UPDATE/DELETE được bảng nào.
- **Đọc**: người dùng đăng nhập Supabase Auth chỉ đọc được workspace có `owner_id = auth.uid()`.
- `outbox_event`, `job_event`, `run_event`, `schema_migrations` là nội bộ; SSE được đọc qua API có kiểm tra quyền và tombstone hiện tại.

## Khác biệt so với mục 5.2 (cần cập nhật tài liệu)

- `source` thêm `status`, `progress`, `error_json` để theo dõi vòng đời nạp (mục 6.5) và hiển thị trên UI.
- `run` thêm `rerun_of`, `created_by`, `cancel_requested`, `outline_json`, `error_json`, `started_at`, `finished_at`.
- `claim` thêm `ordinal`, `cross_check_status`; `claim_evidence` thêm `position`; `context_block` thêm `truncated`, `trace_json`.
- Thêm bảng `report_section` cho báo cáo tổng hợp; `parse_revision` thêm `page_count`, `leaf_count`; `job` thêm `stage`, `max_attempts`, `error_json`, `updated_at`.
- Node đang được run trích dẫn không xóa được (`on delete restrict`): job GC của nguồn đã xóa phải che nội dung thay vì xóa dòng.
