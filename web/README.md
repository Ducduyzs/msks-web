# MSKS Web

Frontend của MSKS: React 19 + Vite + TypeScript, React Router, TanStack Query, Tailwind CSS v4.
Hợp đồng dữ liệu theo [../ARCHITECTURE.md](../ARCHITECTURE.md) bản 0.3; mục 0.1 ghi stack MVP thực tế.

## Chạy

```bash
npm install
npm run dev        # http://localhost:5173
npm test           # Vitest: logic + giao diện (jsdom)
npm run build      # type-check + build vào dist/
npm run lint
```

Mặc định ứng dụng dùng **API giả lập** trong trình duyệt (`VITE_API_MODE=mock`), không cần backend.
Gọi FastAPI thật: tạo `.env.local` từ [.env.example](.env.example) với `VITE_API_MODE=http`.

### Cờ tính năng

| Biến | Tính năng | Mặc định |
|---|---|---|
| `VITE_FEATURE_REMOTE_SOURCES` | URL / DOI / HTML / DOCX (G3) | bật ở mock, tắt ở http |
| `VITE_FEATURE_SYNTHESIS` | Tab Tổng hợp, xuất DOCX/PDF (G4) | bật ở mock, tắt ở http |

Tính năng sau MVP luôn có nhãn **Thử nghiệm** trên giao diện.

### Kịch bản thử trong chế độ mock

| Thao tác | Kết quả |
|---|---|
| Hỏi "RAPTOR xây dựng chỉ mục như thế nào?" | Đủ 3 nhãn claim, một claim đối chứng chưa hoàn tất |
| Chọn chỉ S3 + hỏi câu có "so sánh" | `insufficient_evidence` (thiếu một phía so sánh) |
| Câu hỏi chứa `[invalid]` / `[verifier-down]` / `[fail]` | `model_output_invalid` / `verification_unavailable` / run FAILED |
| Tiêu đề mục dàn ý chứa `[fail]` | Báo cáo PARTIAL, mục đó để trống |
| Tải file tên có `scan` | Nạp thất bại `pdf_no_text_layer` |
| Bấm "Hủy lượt chạy" khi đang chạy | Run CANCELLED, không publish thêm |
| Xóa S1 sau khi đã hỏi | Run cũ giữ tham chiếu nhưng ẩn nội dung, trình xem trả 410 |

## Hợp đồng phía client

- **Offset** `char_*`/`quote_*` là Unicode code point; `lib/codepoints.ts` đổi sang UTF-16 khi render (có test emoji, dấu tiếng Việt tổ hợp).
- **SSE** `GET /runs/{id}/stream`: chỉ `stage`, `claim_verified`, `section`, `done`, `error`, mỗi event có `seq`. `EventSource` tự kết nối lại với `Last-Event-ID`; `useRunStream` dedup theo `seq`. Khi nhận `done`, client đọc lại `GET /runs/{id}` (dữ liệu đã persist).
- **Ghi** gửi `X-CSRF-Token` từ cookie `csrf_token`; upload/QA/synthesis gửi `Idempotency-Key` (thử lại cùng nội dung dùng lại khóa).
- **Lỗi** đọc thân `{code, message, retryable, trace_id}`; UI hiện mã lỗi và trace, chỉ cho "Thử lại" khi `retryable`.
- **Nhãn**: "Được bằng chứng hỗ trợ" / "Có đối chứng độc lập" / "Có bằng chứng mâu thuẫn". Điểm NLI và độ tự tin của LLM chỉ hiện trong tab kiểm toán, không như độ chắc chắn của đáp án.
- **Nguồn đã xóa**: không mở lại nội dung qua trình xem, kiểm toán hay xuất.
- Nội dung nguồn render dạng text thuần (React escape), không chèn HTML.

### Endpoint client dùng nhưng bảng mục 10 chưa liệt kê

Backend cần bổ sung hoặc xác nhận:

| Endpoint | Dùng cho |
|---|---|
| `GET /api/workspaces` | Danh sách workspace |
| `GET /api/workspaces/{id}/runs?cursor=` | Lịch sử, trả `{items, next_cursor}` |
| `GET /api/ready` → `{ready, components, llm_provider, calibrated}` | Chấm trạng thái, thông báo gửi dữ liệu ra provider |
| `POST /api/workspaces/{id}/qa` nhận `rerun_of` | "Chạy lại cùng cấu hình" |

Auth đã dùng Supabase và cookie phiên cùng origin (mục 0.1). Production cần reverse proxy `/api` và SPA fallback; `npm run build` chỉ tạo static assets, không triển khai backend/proxy.

## Cấu trúc

```text
src/
├── api/
│   ├── types.ts         # hợp đồng dữ liệu
│   ├── client.ts        # interface Api + HttpApi (fetch, CSRF, idempotency, EventSource)
│   ├── mock.ts          # MockApi: cùng interface, mô phỏng job, SSE seq, hủy, tombstone
│   ├── mockData.ts      # nguồn và claim mẫu
│   └── index.ts         # chọn mock/http
├── hooks/               # useSources, useRuns (cursor), useReadiness, useRunStream
├── lib/                 # codepoints, labels, features
├── components/
│   ├── viewer.tsx       # ngăn kéo xem nguồn: tô sáng leaf + quote theo code point, fallback khi không ánh xạ được
│   ├── ClaimCard.tsx    # claim, chip trích dẫn, ghi chú đối chứng chéo, hai phía khi mâu thuẫn
│   ├── RunOptions.tsx   # phạm vi nguồn, ngân sách, thông báo provider
│   └── run/             # RunView, tiến trình, chỉ số mục 8.4, đồng thuận, kiểm toán, xuất
├── pages/               # Workspaces, Nguồn, Hỏi đáp, Tổng hợp, Lịch sử, Chi tiết run
└── router.tsx
```
