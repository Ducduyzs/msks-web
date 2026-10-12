# Bảo mật MSKS

Tài liệu cho người phát triển và người triển khai: hệ thống chống lại những gì, cấu hình bắt buộc khi đưa lên
mạng, và rủi ro còn lại. Cập nhật 10/10/2026.

## 1. Các lớp bảo vệ

| Mối đe dọa | Lớp bảo vệ | Mã |
|---|---|---|
| Đọc/sửa dữ liệu workspace người khác | Mọi route kiểm quyền sở hữu; trả 404 thay vì 403 để không lộ sự tồn tại; RLS trên mọi bảng; media chỉ đọc qua URL ký 300 s cấp sau kiểm quyền | `api_deps.require_*`, `db/migrations` |
| Giả mạo yêu cầu (CSRF) | Cookie phiên HttpOnly `SameSite=Lax`, path `/api`; ghi phải kèm `X-CSRF-Token` khớp cookie (double submit) và `Origin` nằm trong danh sách | `auth.current_user`, `auth.check_origin` |
| Token bị đánh cắp sau khi đăng xuất | JWT còn hạn nhưng `session_id` không còn trong `auth.sessions` → 401 `session_revoked` (cache 15 s). Đăng xuất gọi Supabase để thu hồi phiên + refresh token | `security.enforce_session`, `DELETE /api/auth/session` |
| Lộ mật khẩu | Xác thực hai lớp TOTP; tài khoản đã bật thì **server** từ chối mọi token chưa ở mức `aal2` (cả khi gọi API trực tiếp) | `security.enforce_session`, `/security` |
| Quên đăng xuất / máy dùng chung | Tự đăng xuất sau 30 phút không hoạt động (đồng bộ giữa các tab, báo trước 1 phút); "Đăng xuất khỏi mọi thiết bị" | `useIdleLogout`, `SecurityPage` |
| Spam / đốt chi phí LLM / dò token | Giới hạn tần suất cửa sổ trượt trong Postgres theo người dùng (hỏi đáp 20/phút + 300/ngày, tổng hợp, upload, URL media…) và theo IP (đổi token 20/phút); 429 + `Retry-After` | `security.RATE_LIMITS`, `api_deps.limit` |
| DoS bộ nhớ bằng thân request lớn | Từ chối trước khi đọc: JSON > 1 MB, multipart > giới hạn upload + 1 MB | `api.trace_middleware` |
| XSS | React escape mặc định, không dùng `dangerouslySetInnerHTML`; CSP bản build `script-src 'self'` (không `unsafe-inline`/`unsafe-eval`), `object-src 'none'`; link ngoài chỉ `http(s)` | `web/src/lib/csp.ts`, `safeExternalUrl` |
| Clickjacking, đoán MIME, rò URL | `frame-ancestors 'none'` + `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`; API trả `Cache-Control: no-store` | `security.apply_security_headers`, `vite.config.ts` |
| File độc hại tải lên | Kiểm tra MIME thực (sniff), giới hạn kích thước/trang; video qua phiên upload, ffmpeg chạy bằng tham số danh sách + timeout; tên file được làm sạch | `ingest.sniff`, `api_media` |
| Dịch tài liệu làm lộ/biến dạng nội dung | Bản dịch chỉ đọc qua API có kiểm quyền + tombstone (RLS không cấp quyền đọc); xóa nguồn xóa luôn bản dịch; prompt coi đoạn là dữ liệu (đã thử câu chèn lệnh); bản dịch không vào index/QA; cảnh báo số liệu đổi; giới hạn 300 000 ký tự + 20 bản dịch/giờ | `translation.py`, `api_translation.py` |
| Prompt injection trong tài liệu/bài giảng | Prompt coi evidence là dữ liệu; mọi claim phải qua NLI trên đoạn hiển thị, chốt số liệu/so sánh cho tiếng Việt | `qa.grounded_prompt`, `vi_text.claim_guard` |
| Dò tài khoản | Thông báo đăng nhập không phân biệt sai email hay sai mật khẩu | `AuthGate` |
| Truy vết sự cố | Nhật ký sự kiện bảo mật (đăng nhập vào app, đăng xuất, bị chặn CSRF/tần suất/2FA/phiên đã thu hồi) — người dùng xem của mình ở `/security`; giữ 180 ngày | `security_event` (migration 0009) |
| Thư viện có lỗ hổng | `npm audit` 0 lỗ hổng; `pip-audit` sạch với các gói backend dùng trực tiếp (đã nâng `python-multipart`, `cryptography`, `pypdf`, `pillow`, `urllib3`…) | `pyproject.toml` |

## 2. Bắt buộc khi triển khai lên mạng

1. **HTTPS** cho cả frontend và API; đặt `MSKS_COOKIE_SECURE=true` (API ghi log lỗi nếu origin là https mà biến này tắt).
2. `MSKS_CORS_ORIGINS` = đúng origin frontend thật, không dùng `*`.
3. Đứng sau reverse proxy/CDN → `MSKS_TRUSTED_PROXY_HOPS` = số proxy (thường 1). Đặt sai cho phép giả `X-Forwarded-For` để né giới hạn theo IP; để 0 sau proxy thì mọi người dùng chung một IP.
4. Máy chủ tĩnh của frontend phải gửi header trong `web/dist/_headers` (Netlify/Cloudflare Pages tự đọc; nginx: chép từng dòng thành `add_header … always;`). Đặt `VITE_PUBLIC_ORIGIN=https://…` trước khi build để có HSTS.
5. Không đặt khóa secret Supabase/OpenAI trong `web/`; chỉ `.env` của backend.
6. API và worker dùng chung một `.env`: thiết lập ảnh hưởng kết quả (model, ngưỡng, chia đoạn) nằm trong `config_hash`; lệch nhau thì lượt hỏi bị từ chối.

## 3. Cấu hình nên bật trên Supabase (Dashboard → Authentication)

- **MFA → TOTP**: bật (mặc định đã bật trên dự án mới).
- **Password**: độ dài tối thiểu ≥ 12; bật *Leaked password protection* nếu gói cho phép (frontend chỉ kiểm tra gợi ý).
- **Email**: bật xác nhận email khi đăng ký.
- **JWT expiry**: giữ 3600 s hoặc ngắn hơn — sau đăng xuất, token chỉ còn dùng được tối đa 15 s nhờ kiểm tra phiên, nhưng thời hạn ngắn vẫn giảm thiệt hại khi `MSKS_SESSION_CHECKS_ENABLED=false`.
- **Rate limits** của Auth (đăng nhập, gửi email): giữ mặc định hoặc chặt hơn — đăng nhập bằng mật khẩu đi thẳng tới Supabase, không qua giới hạn của MSKS.

## 4. Rủi ro còn lại

- **Refresh token trong `localStorage`** (mặc định của supabase-js): nếu có XSS, token bị đọc được. Giảm thiểu bằng CSP chặt; thu hồi bằng "Đăng xuất khỏi mọi thiết bị".
- **Thu hồi phiên trễ ≤ 15 s** do cache trong từng tiến trình API.
- **Giới hạn tần suất fail-open**: Postgres lỗi thì không chặn (ưu tiên sẵn sàng). Giới hạn đăng nhập mật khẩu do Supabase quản lý.
- **`docling`** còn lỗ hổng đã công bố (bản sửa 2.131): chưa nâng vì có thể đổi kết quả parse — cần chạy lại hồi quy tài liệu trước khi nâng.
- `.venv` dev được tạo kèm site-packages toàn cục nên `pip-audit` liệt kê cả gói của dự án khác (jupyter, langchain…). Môi trường triển khai nên là venv/container riêng chỉ cài `pyproject.toml`.
- Chưa kiểm thử Safari/Firefox; chưa có kiểm thử xâm nhập độc lập.

## 5. Kiểm thử

| Lệnh | Nội dung | Kết quả 10/10 |
|---|---|---|
| `pytest -q tests/test_security.py` | header, 413, CORS, IP giả, cửa sổ trượt, fail-open, phiên thu hồi, 2FA, gộp nhật ký, `config_hash` | 14/14 (cả bộ: 108/108) |
| `python scripts/e2e_security.py` | đóng vai kẻ tấn công trên API + Supabase thật: thiếu CSRF, sai Origin, thân 2 MB, dùng lại token sau đăng xuất, thiết bị khác sau "đăng xuất mọi nơi", bật 2FA bằng TOTP thật rồi dùng token chỉ có mật khẩu, nhật ký, spam đổi token | 21/21 |
| `cd web && npm test` | lọc URL, mật khẩu, CSP, tự đăng xuất (kể cả đồng bộ tab), 401 → đăng xuất | 50/50 |
| Chromium headless trên bản build có CSP | bật 2FA qua QR, đăng nhập lại bị đòi mã, mã sai bị từ chối, đăng xuất mọi thiết bị; luồng bài giảng (upload trực tiếp lên Storage, ảnh, âm thanh, citation) | 11/11 và 17/17, 0 vi phạm CSP |

Chạy liên tiếp nhiều script e2e từ một IP có thể gặp 429 (đúng thiết kế): đợi 2 phút hoặc đặt `MSKS_RATE_LIMIT_SCALE=10` cho môi trường thử.
