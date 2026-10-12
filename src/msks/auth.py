"""Xác thực bằng Supabase Auth (JWT ký bất đối xứng, kiểm qua JWKS).

Client đăng nhập với Supabase, gửi access token tới POST /api/auth/session; server kiểm tra rồi đặt
cookie HttpOnly cùng origin (mục 10). Request sau dùng cookie (kể cả SSE) hoặc header Bearer.
Với cookie, mọi request ghi phải kèm X-CSRF-Token khớp cookie csrf_token (double submit).
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Request

from .errors import AppError, unauthorized
from .security import enforce_session, record_event
from .settings import get_settings

SESSION_COOKIE = "msks_session"
CSRF_COOKIE = "csrf_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def check_origin(request: Request) -> None:
    """Reject browser writes from origins outside the configured frontend."""
    origin = request.headers.get("origin")
    allowed = {o.strip().rstrip("/") for o in get_settings().cors_origins.split(",") if o.strip()}
    allowed.add(str(request.base_url).rstrip("/"))
    if origin is not None and origin.rstrip("/") not in allowed:
        record_event("csrf_rejected", None, request, {"reason": "origin", "origin": origin[:200]})
        raise AppError(403, "csrf_failed", "Origin không được phép.")
    if origin is None and request.headers.get("sec-fetch-site") == "cross-site":
        record_event("csrf_rejected", None, request, {"reason": "cross_site"})
        raise AppError(403, "csrf_failed", "Yêu cầu khác origin không được phép.")


@dataclass(frozen=True)
class User:
    id: str
    email: str | None
    expires_at: int
    session_id: str | None = None   # auth.sessions.id — dùng để chặn phiên đã đăng xuất
    aal: str = "aal1"               # aal2 = đã qua xác thực hai lớp


@lru_cache
def _jwks_client() -> jwt.PyJWKClient:
    settings = get_settings()
    url = settings.supabase_jwks_url or f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=600)


def verify_token(token: str) -> User:
    settings = get_settings()
    try:
        key = _jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["ES256", "RS256", "EdDSA"],
            audience="authenticated",
            issuer=f"{settings.supabase_url.rstrip('/')}/auth/v1",
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise unauthorized("Phiên đăng nhập đã hết hạn.") from exc
    except (jwt.PyJWTError, jwt.PyJWKClientError) as exc:
        raise unauthorized("Token không hợp lệ.") from exc
    if claims.get("role") != "authenticated":
        raise unauthorized("Token không thuộc người dùng đã đăng nhập.")
    return User(id=str(claims["sub"]), email=claims.get("email"), expires_at=int(claims["exp"]),
                session_id=claims.get("session_id"), aal=str(claims.get("aal") or "aal1"))


def current_user(request: Request) -> User:
    """Dependency FastAPI: lấy người dùng từ header Bearer hoặc cookie phiên."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        user = verify_token(header[7:].strip())
        enforce_session(user, request)
        return user
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise unauthorized()
    if request.method not in SAFE_METHODS:
        check_origin(request)
        expected = request.cookies.get(CSRF_COOKIE, "")
        sent = request.headers.get("x-csrf-token", "")
        if not expected or not hmac.compare_digest(expected.encode(), sent.encode()):
            record_event("csrf_rejected", None, request, {"reason": "token"})
            raise AppError(403, "csrf_failed", "Thiếu hoặc sai CSRF token.")
    user = verify_token(token)
    enforce_session(user, request)
    return user
