"""Đọc biến môi trường DB từ .env ở gốc — không đọc cấu hình frontend."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in (ROOT / ".env",):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    values.update({k: v for k, v in os.environ.items() if k in ("DATABASE_URL", "DIRECT_URL")})
    return values


def migration_url() -> str:
    env = load_env()
    url = env.get("DIRECT_URL") or env.get("DATABASE_URL")
    if not url:
        raise SystemExit("Thiếu DIRECT_URL/DATABASE_URL trong .env")
    return url
