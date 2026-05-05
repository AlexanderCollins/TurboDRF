"""
Postgres test settings — used to verify RLS integration tests against a real
Postgres backend. Inherits everything from tests.settings; overrides DATABASES.

Run with:
    DJANGO_SETTINGS_MODULE=tests.settings_pg pytest tests/integration/test_rls.py
"""

import os

from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("TURBODRF_TEST_PG_DB", "turbodrf"),
        # IMPORTANT: app_user is NON-SUPERUSER. Postgres superusers bypass
        # RLS entirely (even with FORCE) — the production app should never
        # connect as a superuser. Use a regular role for testing.
        "USER": os.environ.get("TURBODRF_TEST_PG_USER", "app_user"),
        "PASSWORD": os.environ.get("TURBODRF_TEST_PG_PASSWORD", "app_user"),
        "HOST": os.environ.get("TURBODRF_TEST_PG_HOST", "127.0.0.1"),
        "PORT": os.environ.get("TURBODRF_TEST_PG_PORT", "5433"),
    }
}
