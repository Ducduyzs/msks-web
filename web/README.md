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
| Workspace **Demo: Bài giảng tiếng Việt** | Bài giảng mẫu (bản chép thật từ pipeline, còn lỗi ASR) — hỏi “Lập chỉ mục 70 bài báo mất bao lâu?” để thấy citation theo thời gian; bấm “Sửa bản chép” để tạo phiên bản mới |
| Tải video MP4/WebM bất kỳ | Mô phỏng tải theo phần + pipeline; mọi video nhận bản chép mẫu (mock không xử lý video thật) |
| Thêm YouTube vào workspace tài liệu rồi hỏi “tất cả nguồn” | `422 profile_index_missing` — bài giảng và tài liệu khác model profile |
| Bấm "Hủy lượt chạy" khi đang chạy | Run CANCELLED, không publish thêm |
| Xóa S1 sau khi đã hỏi | Run cũ giữ tham chiếu nhưng ẩn nội dung, trình xem trả 410 |

## Giao diện đa ngôn ngữ và chủ đề

- **Ngôn ngữ giao diện:** Tiếng Việt (gốc), English, 简体中文, 日本語, 한국어, Français, Deutsch, Español — chọn ở nút
  bảng màu trên thanh trên cùng hoặc trang đăng nhập; lưu trong trình duyệt, mặc định theo ngôn ngữ trình duyệt.
- **Chủ đề:** Sáng / Tối / Theo hệ thống; 6 màu nhấn (đã kiểm tra tương phản cho cả hai nền); 3 cỡ chữ. Màu trạng
  thái claim (hỗ trợ / đối chứng / mâu thuẫn) cố định để không đổi nghĩa.

Chữ giao diện viết bằng tiếng Việt và bọc trong `t('…')` (biến: `t('Đã tạo nguồn {0}', { 0: alias })`); bảng nhãn ở
mức module dùng `translated({ key: msg('…') })` để dịch lúc đọc. Khi thêm/sửa chữ:

```bash
npm run i18n:extract                       # cập nhật src/i18n/catalog.json (kèm file dùng mỗi câu làm ngữ cảnh)
python ../scripts/translate_ui.py          # dịch câu mới sang 7 ngôn ngữ bằng LLM backend; giữ bản đã sửa tay
npm run i18n:extract -- --check            # CI: báo lỗi nếu catalog lỗi thời hoặc ngôn ngữ nào thiếu câu
```

Tiếng Anh đã được rà và sửa tay (~110 câu sai nghĩa theo ngữ cảnh sản phẩm) và dùng làm tham chiếu nghĩa khi dịch các
ngôn ngữ khác; các ngôn ngữ còn lại là bản máy — giao diện ghi rõ — nên nhờ người bản ngữ rà. Test
`src/i18n/i18n.test.tsx` chặn từ điển thiếu câu hoặc làm hỏng biến `{0}`.

## Dịch tài liệu

Trình xem nguồn (tài liệu và bài giảng) có tab **Bản dịch**: chọn ngôn ngữ đích (15 ngôn ngữ, gồm Anh-Mỹ/Anh-Anh, Trung
giản thể/phồn thể, Nhật, Hàn, Pháp, Đức…) → dịch theo từng đoạn lá ở backend → xem song song hoặc chỉ bản dịch, tải
`.txt`. Bản dịch **chỉ để đọc**: trích dẫn và kiểm chứng luôn dựa trên bản gốc; đoạn có số liệu khác bản gốc được cảnh
báo. Với bài giảng, mốc thời gian trong bản dịch tua được âm thanh/khung hình.

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
│   ├── mockLecture.ts   # bài giảng mẫu: bản chép, khung hình SVG, đọc SRT/VTT
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
