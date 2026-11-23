"""
Minimal Django settings for running the ``allowances_django`` test suite.

Core tests on ``MemoryStore`` do not use these settings.

The suite passes on SQLite and PostgreSQL. ``pytest`` runs against SQLite by default;
``tests/env.py`` (gitignored, copied from ``tests/env.example.py``) selects a local PostgreSQL.
``ALLOWANCES_TEST_DB=sqlite`` forces SQLite when that file exists.
"""

import os

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

SQLITE = dict(
    ENGINE="django.db.backends.sqlite3",
    NAME=str(BASE_DIR / "allowances.sqlite3"),
    # The options allowances.W001 requires of an SQLite deployment.
    OPTIONS={
        "transaction_mode": "IMMEDIATE",
        "timeout": 20,
        "init_command": "PRAGMA journal_mode=WAL;",
    },
    # File-backed test database: the concurrency test uses a connection from a second thread, and
    # in-memory shared-cache SQLite locks differently from a file.
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
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.admin",
    "allowances_django",
]

# What django.contrib.admin requires; the admin tests call the ModelAdmin classes directly.
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
ROOT_URLCONF = None

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Empty default; tests pass definitions with SettingsProvider(config=...).
ALLOWANCES = {"currencies": {}, "features": {}}
