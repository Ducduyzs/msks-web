"""Gia cố bảo mật API (msks.security): header, giới hạn kích thước/tần suất, phiên đã thu hồi, bắt buộc 2FA."""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from msks import api, auth, security
from msks.errors import AppError
from msks.settings import Settings, get_settings


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()

    def set_(**values):
        for key, value in values.items():
            monkeypatch.setattr(s, key, value)
    return set_


def fake_db(monkeypatch, rows):
    """db.tx() giả: mỗi lệnh execute trả lần lượt các dòng trong `rows` (hoặc ném lỗi nếu phần tử là Exception)."""
    queries = []

    class Conn:
        def execute(self, sql, params=()):
            queries.append((sql, params))
            row = rows.pop(0)
            if isinstance(row, Exception):
                raise row
            return type("Cursor", (), {"fetchone": lambda self: row, "rowcount": 0})()

    @contextmanager
    def tx():
        yield Conn()

    monkeypatch.setattr(security.db, "tx", tx)
    return queries


def request(headers=None, client=("203.0.113.9", 4000)) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "scheme": "http", "server": ("testserver", 80), "path": "/api/x",
                    "headers": raw, "client": client})


# ------------------------------------------------------------------ header


def test_security_headers_on_every_api_response(settings):
    response = TestClient(api.app).get("/api/health")
    for name in ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy", "Content-Security-Policy", "Permissions-Policy"):
        assert name in response.headers
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert "Strict-Transport-Security" not in response.headers  # HTTP dev: không đặt HSTS
    settings(cookie_secure=True)
    assert "max-age=31536000" in TestClient(api.app).get("/api/health").headers["Strict-Transport-Security"]


def test_oversized_json_body_rejected_before_reading():
    response = TestClient(api.app).post("/api/auth/session", headers={"Origin": "http://localhost:5173"},
                                        content=b"{" + b" " * (get_settings().max_json_bytes + 10) + b"}")
    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_cors_only_allows_known_headers():
    response = TestClient(api.app).options("/api/workspaces", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-evil-header"})
    assert response.status_code == 400
    ok = TestClient(api.app).options("/api/workspaces", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-csrf-token"})
    assert ok.status_code == 200
    evil = TestClient(api.app).options("/api/workspaces", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in evil.headers


# ------------------------------------------------------------------ IP + tần suất


def test_client_ip_ignores_spoofed_forwarded_for_unless_proxy_trusted(settings):
    spoofed = request({"X-Forwarded-For": "1.2.3.4"})
    assert security.client_ip(spoofed) == "203.0.113.9"
    settings(trusted_proxy_hops=1)
    assert security.client_ip(request({"X-Forwarded-For": "1.2.3.4, 198.51.100.7"})) == "198.51.100.7"


def test_rate_limit_returns_429_with_retry_after(monkeypatch, settings):
    settings(rate_limit_enabled=True)
    fake_db(monkeypatch, [{"hits": 20, "previous": 0}, {"hits": 21, "previous": 0}])
    security.hit("qa", "user:u")  # lượt thứ 20: còn trong giới hạn 20/phút
    with pytest.raises(AppError) as exc:
        security.hit("qa", "user:u", user_id="u")
    assert exc.value.status == 429 and exc.value.code == "rate_limited"
    assert 0 < int(exc.value.headers["Retry-After"]) <= 60


def test_rate_limit_counts_previous_window_across_boundary(monkeypatch, settings):
    """Dồn lượt quanh ranh giới phút không lách được: 17 lượt cuối phút trước + 10 lượt đầu phút này > 20."""
    settings(rate_limit_enabled=True)
    monkeypatch.setattr(security.time, "time", lambda: 1_800_000_002.0)  # 2 s sau đầu cửa sổ 60 s
    fake_db(monkeypatch, [{"hits": 10, "previous": 17}])
    with pytest.raises(AppError) as exc:
        security.hit("qa", "user:u")
    assert exc.value.code == "rate_limited"
    monkeypatch.setattr(security.time, "time", lambda: 1_800_000_058.0)  # gần hết cửa sổ: phần chồng lấn nhỏ
    fake_db(monkeypatch, [{"hits": 10, "previous": 17}])
    security.hit("qa", "user:u")


def test_rate_limit_fails_open_when_counter_unavailable(monkeypatch, settings):
    settings(rate_limit_enabled=True)
    fake_db(monkeypatch, [RuntimeError("db down")])
    security.hit("qa", "user:u")  # không ném: lỗi bộ đếm không được làm sập API


def test_429_reaches_client_with_retry_after_header(monkeypatch):
    monkeypatch.setattr(api.app, "dependency_overrides", {auth.current_user: lambda: auth.User("u", None, 2_000_000_000)})

    def blocked(*args, **kwargs):
        raise AppError(429, "rate_limited", "Quá nhiều yêu cầu.", retryable=True, headers={"Retry-After": "42"})
    monkeypatch.setattr(security, "hit", blocked)
    response = TestClient(api.app).post("/api/workspaces", json={"name": "x"}, headers={"Origin": "http://localhost:5173"})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "42"
    assert response.json()["retryable"] is True


# ------------------------------------------------------------- phiên + 2FA


def user(aal="aal1", session_id="s1"):
    return auth.User("u", "a@example.com", 2_000_000_000, session_id=session_id, aal=aal)


def test_revoked_session_is_rejected(monkeypatch, settings):
    settings(session_checks_enabled=True)
    security.forget_sessions("u")
    fake_db(monkeypatch, [{"alive": False, "mfa": False}])
    with pytest.raises(AppError) as exc:
        security.enforce_session(user(), None)
    assert exc.value.status == 401 and exc.value.code == "session_revoked"


def test_mfa_enrolled_account_requires_aal2(monkeypatch, settings):
    settings(session_checks_enabled=True)
    security.forget_sessions("u")
    fake_db(monkeypatch, [{"alive": True, "mfa": True}])
    with pytest.raises(AppError) as exc:
        security.enforce_session(user("aal1"), None)
    assert exc.value.code == "mfa_required"
    security.enforce_session(user("aal2"), None)  # cùng phiên trong cache 15 s, đã qua 2FA → được phép


def test_session_status_is_cached_then_forgotten_on_logout(monkeypatch, settings):
    security.forget_sessions("u")
    queries = fake_db(monkeypatch, [{"alive": True, "mfa": False}, {"alive": False, "mfa": False}])
    assert security.session_status("u", "s1") == (True, False)
    assert security.session_status("u", "s1") == (True, False)
    assert len(queries) == 1
    security.forget_sessions("u")  # đăng xuất ở tiến trình này → đọc lại ngay
    assert security.session_status("u", "s1") == (False, False)


def test_token_without_session_id_is_treated_as_revoked(monkeypatch, settings):
    settings(session_checks_enabled=True)
    security.forget_sessions("u")
    fake_db(monkeypatch, [{"alive": True, "mfa": False}])
    with pytest.raises(AppError) as exc:
        security.enforce_session(user(session_id=None), None)
    assert exc.value.code == "session_revoked"


def test_repeated_block_events_are_deduplicated(monkeypatch):
    stored = []
    monkeypatch.setattr(security, "_store_event", lambda *args: stored.append(args[1]))
    monkeypatch.setattr(security, "_recent_events", {})
    for _ in range(5):
        security.record_event("csrf_rejected", None, request())
    security.record_event("session_created", "u", request())
    assert stored == ["csrf_rejected", "session_created"]


# ------------------------------------------------------------------ cấu hình


def test_operational_settings_do_not_change_config_hash():
    base = Settings()
    other = Settings(msks_media_part_bytes=400_000, msks_rate_limit_scale=0.1, msks_cors_origins="https://app.example")
    assert (other.media_part_bytes, other.rate_limit_scale) == (400_000, 0.1)  # giá trị thật sự khác
    assert base.config_hash() == other.config_hash()
    # Thiết lập ảnh hưởng kết quả vẫn đổi hash (worker khác model → run bị từ chối).
    changed = Settings(msks_asr_model="Systran/faster-whisper-medium")
    assert changed.asr_model == "Systran/faster-whisper-medium"
    assert base.config_hash() != changed.config_hash()
