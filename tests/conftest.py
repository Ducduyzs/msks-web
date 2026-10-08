"""Unit tests must not depend on local secrets or connect to a real service."""
import os

from msks.settings import Settings, get_settings

Settings.model_config["env_file"] = None
os.environ.update({
    "DATABASE_URL": "postgresql://test:test@localhost:1/test",
    "SUPABASE_URL": "https://unit-test.invalid",
    "SUPABASE_SECRET_KEY": "unit-test-placeholder",
    "MSKS_ALLOW_UNCALIBRATED": "true",
})
get_settings.cache_clear()
