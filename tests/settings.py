"""
Minimal Django settings for running the ``allowances_django`` test suite.

Test-only scaffolding: it exists to give the app registry and the ORM something to run against.
The core's own tests need none of this — they run against ``MemoryStore`` without Django involved
at all.

The suite is expected to pass on both supported engines. ``pytest`` alone runs against SQLite and
needs no setup whatsoever; ``tests/env.py`` (gitignored, copied from ``tests/env.example.py``)
points it at a local Postgres instead. ``ALLOWANCES_TEST_DB=sqlite`` forces SQLite even when that
file exists, which is how one machine covers both.
"""

import os

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

SQLITE = dict(
    ENGINE="django.db.backends.sqlite3",
    NAME=str(BASE_DIR / "allowances.sqlite3"),
    # The same options the library asks any SQLite deployment for, so the suite tests the
    # configuration people are told to run rather than a friendlier one. See `allowances.W001`.
    OPTIONS={
        "transaction_mode": "IMMEDIATE",
        "timeout": 20,
        "init_command": "PRAGMA journal_mode=WAL;",
    },
    # A file, where Django would default to an in-memory test database. The concurrency test opens
    # a second connection from another thread, and shared-cache in-memory SQLite takes different
    # locks from the file-backed database anyone actually deploys — which would make that test
    # prove nothing.
    TEST=dict(NAME=str(BASE_DIR / "test_allowances.sqlite3")),
)

REQUESTED = os.environ.get("ALLOWANCES_TEST_DB", "").lower()


def _database():
    if REQUESTED == "sqlite":
        return SQLITE
    try:
        from tests.env import DATABASE
    except ImportError as exc:
        if REQUESTED:
            raise ImportError(
                f"ALLOWANCES_TEST_DB={REQUESTED!r} needs tests/env.py. Copy tests/env.example.py "
                f"and fill in your local credentials, or unset the variable to run on SQLite."
            ) from exc
        return SQLITE
    return DATABASE


SECRET_KEY = "allowances-test-suite-not-for-production-use"
DEBUG = True
USE_TZ = True

DATABASES = {"default": _database()}

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "allowances_django",
]

MIDDLEWARE = []
ROOT_URLCONF = None

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Individual tests inject their own definitions via SettingsProvider(config=...); this is only the
# empty default so `SettingsProvider()` has something to read.
ALLOWANCES = {"currencies": {}, "features": {}}
