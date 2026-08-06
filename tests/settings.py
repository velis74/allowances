"""
Minimal Django settings module for running django-licensing's own test suite (pytest-django).

Not shipped to consumers of the library — this is test-only scaffolding, analogous to the
`setup` module in a host project, except here it exists purely to give the app registry and ORM
something to run against.
"""

try:
    from tests.env import DATABASE
except ImportError as exc:  # pragma: no cover - developer setup error, not a code path under test
    raise ImportError(
        "tests/env.py is missing. Copy tests/env.example.py to tests/env.py and fill in your local "
        "Postgres credentials (see DECISIONS.md for why Postgres, not SQLite)."
    ) from exc

SECRET_KEY = "django-licensing-test-suite-not-for-production-use"
DEBUG = True
USE_TZ = True

DATABASES = {"default": DATABASE}

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_licensing",
]

# No routes/middleware are needed to exercise models directly.
MIDDLEWARE = []
ROOT_URLCONF = None

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
