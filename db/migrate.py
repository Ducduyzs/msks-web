"""Áp dụng migration SQL trong db/migrations theo thứ tự tên file.

    python db/migrate.py status     # liệt kê migration đã/chưa áp dụng
    python db/migrate.py up         # áp dụng migration còn thiếu

Mỗi migration chạy trong một transaction riêng: lỗi → rollback toàn bộ file đó.
Checksum được lưu; sửa một migration đã áp dụng sẽ bị từ chối (tạo migration mới thay vì sửa).
Dùng DIRECT_URL (session pooler) vì DDL không chạy ổn qua transaction pooler.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import migration_url  # noqa: E402

MIGRATIONS = Path(__file__).resolve().parent / "migrations"

BOOKKEEPING = """
create table if not exists public.schema_migrations (
  version    text primary key,
  checksum   text not null,
  applied_at timestamptz not null default now()
);
alter table public.schema_migrations enable row level security;
revoke all on public.schema_migrations from anon, authenticated;
"""


def files() -> list[tuple[str, str, str]]:
    out = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        out.append((path.stem, hashlib.sha256(sql.encode()).hexdigest(), sql))
    return out


def applied(conn: psycopg.Connection) -> dict[str, str]:
    return dict(conn.execute("select version, checksum from public.schema_migrations").fetchall())


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command not in ("status", "up"):
        raise SystemExit(__doc__)
    with psycopg.connect(migration_url(), connect_timeout=20, autocommit=True) as conn:
        conn.execute(BOOKKEEPING)
        done = applied(conn)
        for version, checksum, sql in files():
            if version in done:
                if done[version] != checksum:
                    raise SystemExit(f"[lỗi] {version} đã áp dụng nhưng file đã bị sửa (checksum khác).")
                print(f"[đã có]  {version}")
                continue
            if command == "status":
                print(f"[chưa]   {version}")
                continue
            with conn.transaction():
                conn.execute(sql)
                conn.execute("insert into public.schema_migrations (version, checksum) values (%s, %s)", (version, checksum))
            print(f"[áp dụng] {version}")


if __name__ == "__main__":
    main()
