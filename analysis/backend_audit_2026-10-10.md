# Rà soát backend — 10/10/2026

Rà soát mã các luồng auth/API, upload media, reindex, index publication, QA, worker/job và Storage; chạy toàn bộ test backend hiện có. Bản vá nằm trong `src/msks`; không sửa frontend hay cấu hình bí mật.

## Lỗi đã vá

| Khu vực | Lỗi | Bản vá |
|---|---|---|
| Auth | Tạo/xóa cookie phiên không kiểm tra Origin | Chặn Origin ngoài frontend đã cấu hình hoặc origin của API; kiểm tra trước khi xác thực/cập nhật cookie. Cookie write trong dependency cũng kiểm tra Origin. |
| CSRF | Token không phải ASCII có thể làm `compare_digest(str, str)` ném TypeError | So sánh bytes; token sai trả 403. |
| Upload | Cancel/caption có thể đọc trạng thái cũ rồi ghi đè phiên đã complete | Khóa đúng row upload_session trong cùng transaction trước khi kiểm tra trạng thái và ghi. |
| Upload hết hạn | Phiên hết TTL nhưng chưa được sweep vẫn được cấp URL mới hoặc nhận caption | DTO trả expired, không cấp URL; attach caption kiểm tra TTL. |
| Media quota | Hai request khác idempotency key cùng đọc dung lượng cũ | Khóa workspace trước khi đọc usage và tạo reservation. |
| Cancel | Storage xóa lỗi sau khi phiên chuyển cancelled khiến lần thử lại bị 409 | Cho phép gọi cancel lại với phiên cancelled để thử lại cleanup. |
| Reindex | Hai yêu cầu sửa transcript/reindex có thể cùng tạo job và tranh publish | Khóa source và kiểm tra job ingest/reindex đang queued/running trước khi tạo job. |
| Xóa source | Hai request delete có thể cùng thấy source chưa tombstone và tạo hai job | Khóa source trước khi đọc trạng thái để tạo job idempotent. |
| Leaf quota | Kiểm tra quota chỉ trước embedding; nhiều worker vẫn publish vượt trần; thay revision bị tính cộng cả bản cũ | Khóa workspace và kiểm tra số node phân biệt của trạng thái dự kiến ngay trong transaction publish. Chỉ loại generation sẽ bị thay của cùng source/profile. Precheck bỏ source đang được thay để không từ chối nhầm. |
| Storage | Liệt kê chỉ 1.000 object đầu mỗi thư mục | Phân trang theo offset và thứ tự tên ổn định, giữ liệt kê đệ quy. |
| Media GC | Đánh dấu expired/deleted trước khi xóa Storage; lỗi mạng khiến mất cơ hội retry | Xóa Storage trước khi commit trạng thái DB; row lock + SKIP LOCKED, giới hạn 100 row mỗi nhóm mỗi lượt. Thất bại rollback để retry. |
| QA | Retrieval rỗng vẫn gọi reranker/SBERT; model có thể lỗi trên batch rỗng hoặc bị nạp không cần thiết | Bỏ qua hai model khi không có leaf; đi tiếp nhánh no_context. |

## Kiểm chứng

- Trước vá: 79 test đạt.
- Sau vá: `python -m pytest -q` — **94 test đạt**, gồm 15 test hồi quy mới trong `tests/test_backend_audit.py`.
- `git diff --check -- src/msks tests/test_backend_audit.py` đạt.
- Có một cảnh báo deprecation của Starlette TestClient/httpx; không có test thất bại.
- Test dùng fake DB/Storage/model và TestClient không khởi chạy lifespan DB. Chưa chạy integration PostgreSQL, Supabase Storage, GPU, FFmpeg hay LLM thật.

## Giới hạn và việc còn cần xác minh

Đây là kết quả rà soát và vá lỗi cục bộ, không phải chứng nhận toàn hệ thống hết lỗi.

- Cần test PostgreSQL đồng thời với hai connection/worker cho các khóa mới: quota, complete/cancel/caption, reindex và delete. Unit test xác nhận thứ tự thao tác và nhánh lỗi, chưa chứng minh cách ly transaction thật.
- GC giữ transaction trong khi chờ Storage để bảo toàn khả năng retry. Đã giới hạn số row; nếu Storage chậm cần kiến trúc cleanup queue/outbox để giảm thời gian giữ khóa.
- URL upload đã ký có thể còn hiệu lực sau cancel/expiry. File được upload muộn vẫn cần reconciler định kỳ; bản vá không thu hồi token Storage đã phát hành.
- Blob mồ côi do Storage thành công nhưng transaction DB thất bại vẫn cần reconciler. Thay caption hiện chưa có cơ chế transaction xuyên DB/Storage.
- Các khoảng trống đã ghi trong `REVIEW_2026-10-08.md` vẫn cần xử lý riêng: calibration artifact chưa được xác thực nội dung, readiness chưa có heartbeat service độc lập, ngân sách output LLM và hard timeout parser/model chưa đầy đủ.
- Không chạy migration hay tác động dữ liệu dịch vụ thật trong lượt kiểm tra này.
