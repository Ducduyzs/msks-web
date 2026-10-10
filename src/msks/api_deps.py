"""Helper dùng chung cho các router FastAPI: kiểm quyền workspace, cursor, idempotency."""
from __future__ import annotations

import base64
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Depends

from .auth import User, current_user
from .dto import SOURCE_SELECT
from .errors import AppError, not_found

UserDep = Annotated[User, Depends(current_user)]


def _uuid(value: str, what: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise not_found(what) from exc


def require_workspace(conn, workspace_id: str, user: User) -> dict:
    row = conn.execute(
        "select * from public.workspace where id = %s and owner_id = %s", (_uuid(workspace_id, "Workspace"), user.id)
    ).fetchone()
    if not row:
        raise not_found("Workspace")
    return row


def require_owned(conn, table: str, row_id: str, user: User, what: str) -> dict:
    row = conn.execute(
        f"""select t.* from public.{table} t join public.workspace w on w.id = t.workspace_id
            where t.id = %s and w.owner_id = %s""",
        (_uuid(row_id, what), user.id),
    ).fetchone()
    if not row:
        raise not_found(what)
    return row


def encode_cursor(created_at: datetime, row_id) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{row_id}".encode()).decode()


def decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        created, row_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
        parsed = datetime.fromisoformat(created)
        if parsed.tzinfo is None:
            raise ValueError("Cursor timestamp must include timezone")
        return parsed.isoformat(), str(uuid.UUID(row_id))
    except Exception as exc:
        raise AppError(422, "invalid_cursor", "Cursor không hợp lệ.") from exc


def idempotent_job(conn, workspace_id: str, kind: str, key: str | None, request_hash: str) -> dict | None:
    """Cùng key + cùng nội dung → trả job cũ; cùng key khác nội dung → 409 (mục 5.2)."""
    if not key:
        return None
    if len(key) > 200:
        raise AppError(422, "invalid_idempotency_key", "Idempotency-Key quá dài.")
    # Serialize same-key requests for this transaction, including the insert.
    conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{workspace_id}:{kind}:{key}",))
    row = conn.execute(
        "select * from public.job where workspace_id = %s and kind = %s and idempotency_key = %s",
        (workspace_id, kind, key),
    ).fetchone()
    if row and row["request_hash"] != request_hash:
        raise AppError(409, "idempotency_conflict", "Idempotency-Key đã dùng cho một yêu cầu khác.")
    return row


def source_row(conn, source_id) -> dict:
    return conn.execute(SOURCE_SELECT + " where s.id = %s", (source_id,)).fetchone()
