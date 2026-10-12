"""Kiểm thử bảo mật trên hệ thống thật (API + Supabase): đóng vai kẻ tấn công với token/cookie thật.

    python scripts/e2e_security.py [--api http://localhost:8000/api]

Cần API đang chạy. Tạo user tạm, xóa khi xong (nhật ký bảo mật xóa theo). Bước giới hạn tần suất chạy cuối cùng
vì nó chặn đổi token từ IP này trong ≤ 60 s.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import secrets
import struct
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
from e2e_smoke import Admin  # noqa: E402
from msks.settings import get_settings  # noqa: E402

ORIGIN = "http://localhost:5173"
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(("  OK  " if ok else " FAIL ") + label, flush=True)


def totp(secret: str, at: float | None = None) -> str:
    """RFC 6238: HMAC-SHA1, bước 30 s, 6 chữ số — như ứng dụng xác thực trên điện thoại."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = int((at or time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return str((struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000).zfill(6)


def login_cookie(api: str, token: str) -> httpx.Client:
    client = httpx.Client(base_url=api, timeout=30, headers={"Origin": ORIGIN})
    r = client.post("/auth/session", json={"access_token": token})
    r.raise_for_status()
    client.headers["X-CSRF-Token"] = client.cookies["csrf_token"]
    return client


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000/api")
    api = parser.parse_args().api
    settings = get_settings()
    gotrue = settings.supabase_url.rstrip("/") + "/auth/v1"
    apikey = settings.supabase_publishable_key or settings.supabase_secret_key
    admin = Admin()
    email, password = f"msks-sec-{secrets.token_hex(4)}@example.com", secrets.token_urlsafe(18) + "Aa1!"
    user_id = admin.create(email, password)
    bearer = lambda token: {"Authorization": f"Bearer {token}"}  # noqa: E731
    try:
        print("1. Header bảo mật + phiên cookie")
        device = login_cookie(api, admin.sign_in(email, password))
        me = device.get("/auth/me")
        check(me.status_code == 200 and me.json()["aal"] == "aal1" and me.json()["mfa_enabled"] is False, "đăng nhập: /auth/me aal1, chưa bật 2FA")
        headers = me.headers
        check(headers.get("x-frame-options") == "DENY" and "frame-ancestors 'none'" in headers.get("content-security-policy", "")
              and headers.get("cache-control") == "no-store" and headers.get("x-content-type-options") == "nosniff",
              "phản hồi API có X-Frame-Options, CSP, nosniff, no-store")
        session_cookie = device.cookies["msks_session"]

        print("2. CSRF / Origin / kích thước")
        no_csrf = httpx.post(f"{api}/workspaces", json={"name": "x"}, headers={"Origin": ORIGIN},
                             cookies={"msks_session": session_cookie, "csrf_token": device.cookies["csrf_token"]})
        check(no_csrf.status_code == 403 and no_csrf.json()["code"] == "csrf_failed", "ghi bằng cookie nhưng thiếu X-CSRF-Token → 403")
        evil = httpx.post(f"{api}/workspaces", json={"name": "x"},
                          headers={"Origin": "https://evil.example", "X-CSRF-Token": device.cookies["csrf_token"]},
                          cookies={"msks_session": session_cookie, "csrf_token": device.cookies["csrf_token"]})
        check(evil.status_code == 403, "trang khác origin gửi kèm cookie + CSRF → 403")
        big = device.post("/workspaces", content=b'{"name":"' + b"a" * (2 * 1024 * 1024) + b'"}', headers={"Content-Type": "application/json"})
        check(big.status_code == 413, "thân JSON 2 MB → 413 trước khi server đọc vào bộ nhớ")

        print("3. Đăng xuất thu hồi phiên thật sự")
        stolen = session_cookie  # giả sử token bị đánh cắp trước khi người dùng đăng xuất
        out = device.delete("/auth/session")
        check(out.status_code == 204, "đăng xuất → 204")
        reuse = httpx.get(f"{api}/auth/me", headers=bearer(stolen))
        check(reuse.status_code == 401 and reuse.json()["code"] == "session_revoked",
              f"dùng lại token đã đăng xuất (JWT còn hạn) → 401 session_revoked ({reuse.json().get('code')})")

        print("4. Đăng xuất khỏi mọi thiết bị")
        laptop = login_cookie(api, admin.sign_in(email, password))
        phone_token = admin.sign_in(email, password)
        check(httpx.get(f"{api}/auth/me", headers=bearer(phone_token)).status_code == 200, "thiết bị thứ hai đang đăng nhập")
        everywhere = laptop.delete("/auth/session", params={"scope": "global"})
        check(everywhere.status_code == 204, "đăng xuất mọi thiết bị → 204")
        deadline = time.time() + 20
        status = 200
        while time.time() < deadline and status == 200:
            status = httpx.get(f"{api}/auth/me", headers=bearer(phone_token)).status_code
            if status == 200:
                time.sleep(1)
        check(status == 401, "token của thiết bị khác bị từ chối (≤ 15 s)")

        print("5. Xác thực hai lớp (TOTP)")
        token = admin.sign_in(email, password)
        gh = {"apikey": apikey, **bearer(token)}
        factor = httpx.post(f"{gotrue}/factors", headers=gh, json={"factor_type": "totp", "friendly_name": "e2e"}).json()
        challenge = httpx.post(f"{gotrue}/factors/{factor['id']}/challenge", headers=gh).json()
        verified = httpx.post(f"{gotrue}/factors/{factor['id']}/verify", headers=gh,
                              json={"challenge_id": challenge["id"], "code": totp(factor["totp"]["secret"])})
        check(verified.status_code == 200, f"bật 2FA bằng mã TOTP thật ({verified.status_code})")
        aal2 = verified.json()["access_token"]
        password_only = admin.sign_in(email, password)  # kẻ có mật khẩu nhưng không có điện thoại
        blocked = httpx.post(f"{api}/auth/session", json={"access_token": password_only}, headers={"Origin": ORIGIN})
        check(blocked.status_code == 401 and blocked.json()["code"] == "mfa_required", "chỉ có mật khẩu (aal1) → không lấy được phiên: 401 mfa_required")
        blocked_api = httpx.get(f"{api}/workspaces", headers=bearer(password_only))
        check(blocked_api.status_code == 401 and blocked_api.json()["code"] == "mfa_required", "token aal1 gọi API trực tiếp → 401 mfa_required")
        ok = httpx.get(f"{api}/auth/me", headers=bearer(aal2))
        check(ok.status_code == 200 and ok.json()["aal"] == "aal2" and ok.json()["mfa_enabled"] is True, "token đã qua 2FA (aal2) → được phép")
        wrong = httpx.post(f"{gotrue}/factors/{factor['id']}/verify", headers={"apikey": apikey, **bearer(password_only)},
                           json={"challenge_id": httpx.post(f"{gotrue}/factors/{factor['id']}/challenge",
                                                            headers={"apikey": apikey, **bearer(password_only)}).json()["id"],
                                 "code": "000000" if totp(factor["totp"]["secret"]) != "000000" else "111111"})
        check(wrong.status_code >= 400, "mã TOTP sai bị Supabase từ chối")

        print("6. Nhật ký bảo mật")
        events = [e["event"] for e in httpx.get(f"{api}/auth/security-events", headers=bearer(aal2)).json()["items"]]
        for event in ("session_created", "session_revoked", "sessions_revoked_all", "revoked_session_used", "mfa_required"):
            check(event in events, f"nhật ký có '{event}'")

        print("7. Giới hạn tần suất (đổi token theo IP)")
        codes = [httpx.post(f"{api}/auth/session", json={"access_token": "x" * 40}, headers={"Origin": ORIGIN}) for _ in range(25)]
        limited = [r for r in codes if r.status_code == 429]
        check(bool(limited) and limited[0].headers.get("retry-after", "").isdigit(),
              f"spam đổi token: {sum(r.status_code == 401 for r in codes)}×401 rồi {len(limited)}×429 kèm Retry-After")
    finally:
        admin.delete(user_id)
        print("Đã xóa user tạm (nhật ký bảo mật xóa theo).")
    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} kiểm tra đạt.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
