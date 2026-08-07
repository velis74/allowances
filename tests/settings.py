"""
Minimal Django settings for running the ``allowances_django`` test suite.

Test-only scaffolding: it exists to give the app registry and the ORM something to run against.
The core's own tests (``tests/core/``) need none of this — they run against ``MemoryStore``
without Django involved at all.
"""

try:
    from tests.env import DATABASE
except ImportError as exc:  # pragma: no cover - developer setup error, not a code path under test
    raise ImportError(
        "tests/env.py is missing. Copy tests/env.example.py to tests/env.py and fill in your local "
        "Postgres credentials (see DECISIONS.md for why Postgres, not SQLite)."
    ) from exc

SECRET_KEY = "allowances-test-suite-not-for-production-use"
DEBUG = True
USE_TZ = True

DATABASES = {"default": DATABASE}

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
