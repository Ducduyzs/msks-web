"""Unit tests must not depend on local secrets or connect to a real service."""
import os

import pytest

from msks.settings import Settings, get_settings

Settings.model_config["env_file"] = None
os.environ.update({
    "DATABASE_URL": "postgresql://test:test@localhost:1/test",
    "SUPABASE_URL": "https://unit-test.invalid",
    "SUPABASE_SECRET_KEY": "unit-test-placeholder",
    "MSKS_ALLOW_UNCALIBRATED": "true",
    # Rate limit và kiểm tra phiên/2FA đọc Postgres; test của security.py bật lại với DB giả.
    "MSKS_RATE_LIMIT_ENABLED": "false",
    "MSKS_SESSION_CHECKS_ENABLED": "false",
})
get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_security_event_writes(monkeypatch):
    """Nhật ký sự kiện bảo mật ghi vào Postgres; unit test không có DB."""
    from msks import security

    monkeypatch.setattr(security, "_store_event", lambda *args: None)
