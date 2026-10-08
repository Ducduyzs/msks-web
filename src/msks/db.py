"""Kết nối PostgreSQL (Supabase). Một pool cho mỗi process."""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .settings import get_settings

_pool: ConnectionPool | None = None

# Tham số của Prisma/ORM khác mà libpq không hiểu (chuỗi kết nối mẫu của Supabase có `pgbouncer=true`).
_NON_LIBPQ_PARAMS = {"pgbouncer", "connection_limit", "pool_timeout", "schema"}


def libpq_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in _NON_LIBPQ_PARAMS]
    return urlunsplit(parts._replace(query=urlencode(query)))


def open_pool(min_size: int = 1, max_size: int = 8) -> ConnectionPool:
    """Mở pool dùng DATABASE_URL. Transaction pooler (pgbouncer) không hỗ trợ prepared statement."""
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = ConnectionPool(
            libpq_url(settings.database_url),
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "prepare_threshold": None, "autocommit": False},
            open=True,
            timeout=20,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def tx() -> Iterator[Connection[dict[str, Any]]]:
    """Một transaction: commit khi khối kết thúc bình thường, rollback khi có lỗi."""
    with open_pool().connection() as conn:
        with conn.transaction():
            yield conn


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


def append_run_event(conn: Connection, run_id: str, event_type: str, payload: dict[str, Any]) -> int:
    row = conn.execute(
        "select public.append_run_event(%s, %s, %s) as seq", (run_id, event_type, Jsonb(payload))
    ).fetchone()
    return int(row["seq"])


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
