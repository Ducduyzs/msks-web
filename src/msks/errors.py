"""Lỗi chuẩn của API: {code, message, retryable, trace_id} (ARCHITECTURE.md mục 10)."""
from __future__ import annotations


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable

    def body(self, trace_id: str | None = None) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "trace_id": trace_id}


def not_found(what: str = "Tài nguyên") -> AppError:
    # Không phân biệt "không tồn tại" với "không có quyền" để tránh lộ dữ liệu workspace khác.
    return AppError(404, "not_found", f"{what} không tồn tại hoặc bạn không có quyền truy cập.")


def unauthorized(message: str = "Cần đăng nhập.") -> AppError:
    return AppError(401, "unauthorized", message)


def feature_disabled(name: str) -> AppError:
    return AppError(501, "feature_disabled", f"{name} chưa bật trong MVP (giai đoạn sau, mục 16).")


class TransientError(Exception):
    """Lỗi tạm thời trong worker — job được thử lại với backoff."""


class PermanentError(Exception):
    """Lỗi không nên thử lại (file hỏng, vượt giới hạn...)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
