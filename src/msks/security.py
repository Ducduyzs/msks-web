"""Gia cố bảo mật cho API (10/10/2026).

* Header bảo mật trên mọi phản hồi API (chống nhúng iframe, đoán MIME, rò Referer, cache dữ liệu riêng).
* Giới hạn tần suất theo người dùng/IP, đếm trong Postgres để đúng khi chạy nhiều tiến trình API.
* Phiên đã đăng xuất bị từ chối ngay: JWT Supabase còn hạn nhưng `session_id` không còn trong `auth.sessions`.
* Bắt buộc xác thực hai lớp: người dùng đã có TOTP đã xác minh thì token phải ở mức `aal2`.
* Nhật ký sự kiện bảo mật (đăng nhập vào app, đăng xuất mọi thiết bị, bị chặn) — người dùng xem của chính mình.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from fastapi import Request

from . import db
from .errors import AppError
from .settings import get_settings

log = logging.getLogger("msks.security")

# --------------------------------------------------------------------- header

API_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    # API chỉ trả JSON/SSE/file tải về: không cho chạy script hay bị nhúng nếu trình duyệt lỡ render.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
}


def apply_security_headers(headers, path: str) -> None:
    for name, value in API_SECURITY_HEADERS.items():
        headers.setdefault(name, value)
    if path.startswith("/api/"):
        # Dữ liệu workspace là riêng tư: không để proxy/trình duyệt lưu bản sao.
        headers.setdefault("Cache-Control", "no-store")
    if get_settings().cookie_secure:
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


# ------------------------------------------------------------------------- IP


def client_ip(request: Request) -> str:
    """IP thật của client. Chỉ tin X-Forwarded-For khi đứng sau N proxy đã cấu hình (MSKS_TRUSTED_PROXY_HOPS);
    nếu không, kẻ tấn công tự đặt header này để né giới hạn theo IP."""
    hops = get_settings().trusted_proxy_hops
    if hops > 0:
        chain = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(chain) >= hops:
            return chain[-hops][:64]
    return (request.client.host if request.client else "unknown")[:64]


# ------------------------------------------------------------- rate limiting

# tên → (số lượt, cửa sổ giây). Nhắm vào thao tác tốn tiền (LLM), tốn tài nguyên (upload, xử lý) hoặc nhạy cảm.
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "auth_session": (20, 60),          # theo IP: đổi token lấy cookie
    "qa": (20, 60),                    # mỗi câu hỏi gọi LLM
    "qa_daily": (300, 86_400),
    "synthesis": (5, 60),
    "synthesis_daily": (50, 86_400),
    "upload": (60, 3600),
    "media_session": (30, 3600),
    "media_url": (600, 60),            # URL ký cho audio/khung hình
    "transcript_edit": (60, 3600),
    "workspace_create": (30, 3600),
    "source_delete": (120, 3600),
    "settings_write": (60, 3600),      # glossary, reindex
    "export": (60, 3600),
    "translate": (20, 3600),           # mỗi bản dịch gọi LLM trên cả tài liệu
}


@dataclass(frozen=True)
class Limit:
    name: str
    limit: int
    window: int


def _limit(name: str) -> Limit:
    count, window = RATE_LIMITS[name]
    scale = get_settings().rate_limit_scale
    return Limit(name, max(1, int(count * scale)), window)


def hit(name: str, subject: str, request: Request | None = None, user_id: str | None = None) -> None:
    """Đếm một lượt; vượt giới hạn → 429 kèm Retry-After. Lỗi DB không chặn người dùng (fail-open có log)."""
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return
    rule = _limit(name)
    now = time.time()
    window_start = int(now) - int(now) % rule.window
    key = f"{name}:{subject}"[:200]
    try:
        with db.tx() as conn:
            row = conn.execute(
                """with up as (
                     insert into public.rate_limit_bucket (key, window_start, hits) values (%s, to_timestamp(%s), 1)
                     on conflict (key, window_start) do update set hits = public.rate_limit_bucket.hits + 1
                     returning hits)
                   select (select hits from up) as hits,
                          coalesce((select hits from public.rate_limit_bucket
                                    where key = %s and window_start = to_timestamp(%s)), 0) as previous""",
                (key, window_start, key, window_start - rule.window),
            ).fetchone()
    except Exception:  # noqa: BLE001 — không để lỗi bộ đếm làm sập API
        log.warning("rate limit không đếm được cho %s", name, exc_info=True)
        return
    # Cửa sổ trượt xấp xỉ: cửa sổ cố định cho phép dồn gấp đôi giới hạn quanh ranh giới phút (đo 10/10:
    # 25 lượt đổi token trong vài giây lọt qua vì chia 17 + 10 sang hai phút). Tính thêm phần cửa sổ trước còn chồng lấn.
    overlap = 1 - (now - window_start) / rule.window
    hits = row["hits"] + row["previous"] * overlap
    if hits > rule.limit:
        retry = max(1, int(window_start + rule.window - now))
        record_event("rate_limited", user_id, request, {"limit": name})
        raise AppError(429, "rate_limited", f"Quá nhiều yêu cầu ({name}). Thử lại sau {retry} giây.", retryable=True,
                       headers={"Retry-After": str(retry)})


# ------------------------------------------------------ phiên + xác thực 2 lớp

_SESSION_TTL = 15.0
_session_cache: dict[tuple[str, str], tuple[float, bool, bool]] = {}
_cache_lock = threading.Lock()


def session_status(user_id: str, session_id: str | None) -> tuple[bool, bool]:
    """(phiên còn sống, người dùng có 2FA đã xác minh). Cache 15 s: đăng xuất có hiệu lực trong ≤ 15 s mọi nơi."""
    key = (user_id, session_id or "")
    now = time.monotonic()
    with _cache_lock:
        cached = _session_cache.get(key)
        if cached and cached[0] > now:
            return cached[1], cached[2]
    with db.tx() as conn:
        row = conn.execute(
            """select exists(select 1 from auth.sessions s where s.id = %s and s.user_id = %s
                             and (s.not_after is null or s.not_after > now())) as alive,
                      exists(select 1 from auth.mfa_factors f where f.user_id = %s and f.status = 'verified') as mfa""",
            (session_id, user_id, user_id),
        ).fetchone()
    alive, mfa = bool(row["alive"]) if session_id else False, bool(row["mfa"])
    with _cache_lock:
        if len(_session_cache) > 10_000:
            _session_cache.clear()
        _session_cache[key] = (now + _SESSION_TTL, alive, mfa)
    return alive, mfa


def forget_sessions(user_id: str) -> None:
    with _cache_lock:
        for key in [k for k in _session_cache if k[0] == user_id]:
            _session_cache.pop(key, None)


def enforce_session(user, request: Request | None) -> None:
    """Gọi sau khi chữ ký JWT hợp lệ: chặn phiên đã thu hồi và token chưa qua 2FA khi tài khoản đã bật 2FA."""
    if not get_settings().session_checks_enabled:
        return
    alive, mfa = session_status(user.id, user.session_id)
    if not alive:
        record_event("revoked_session_used", user.id, request)
        raise AppError(401, "session_revoked", "Phiên đăng nhập đã bị đăng xuất. Hãy đăng nhập lại.")
    if mfa and user.aal != "aal2":
        record_event("mfa_required", user.id, request)
        raise AppError(401, "mfa_required", "Tài khoản đã bật xác thực hai lớp: nhập mã từ ứng dụng xác thực để tiếp tục.")


# ------------------------------------------------------------- nhật ký sự kiện

_recent_events: dict[tuple[str, str], float] = {}


def record_event(event: str, user_id: str | None, request: Request | None, detail: dict | None = None) -> None:
    """Ghi sự kiện bảo mật; gộp sự kiện bị chặn lặp lại (cùng loại, cùng người/IP) trong 60 s để không bị spam bảng."""
    ip = client_ip(request) if request else None
    if event in ("csrf_rejected", "rate_limited", "mfa_required", "revoked_session_used"):
        key = (event, user_id or ip or "")
        now = time.monotonic()
        with _cache_lock:
            if _recent_events.get(key, 0) > now:
                return
            if len(_recent_events) > 10_000:
                _recent_events.clear()
            _recent_events[key] = now + 60
    agent = (request.headers.get("user-agent", "")[:300] if request else None) or None
    _store_event(user_id, event, ip, agent, detail or {})
    log.info("security_event %s user=%s ip=%s", event, user_id, ip)


def _store_event(user_id: str | None, event: str, ip: str | None, agent: str | None, detail: dict) -> None:
    try:
        with db.tx() as conn:
            conn.execute(
                "insert into public.security_event (user_id, event, ip, user_agent, detail) values (%s, %s, %s, %s, %s)",
                (user_id, event, ip, agent, db.jsonb(detail)),
            )
    except Exception:  # noqa: BLE001 — nhật ký lỗi không được chặn request
        log.warning("không ghi được sự kiện bảo mật %s", event, exc_info=True)


def recent_events(user_id: str, limit: int = 30) -> list[dict]:
    with db.tx() as conn:
        rows = conn.execute(
            """select event, created_at, ip, user_agent, detail from public.security_event
               where user_id = %s order by created_at desc limit %s""", (user_id, limit)).fetchall()
    return [{"event": r["event"], "created_at": r["created_at"].isoformat(), "ip": r["ip"],
             "user_agent": r["user_agent"], "detail": r["detail"]} for r in rows]


def sweep() -> dict[str, int]:
    """Dọn bộ đếm cũ (> 2 ngày) và nhật ký quá hạn lưu (mặc định 180 ngày). Worker gọi định kỳ."""
    days = get_settings().security_event_retention_days
    with db.tx() as conn:
        buckets = conn.execute("delete from public.rate_limit_bucket where window_start < now() - interval '2 days'").rowcount
        events = conn.execute("delete from public.security_event where created_at < now() - make_interval(days => %s)",
                              (days,)).rowcount
    return {"rate_limit_buckets": buckets, "security_events": events}
