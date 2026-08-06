"""
Template for `tests/env.py` — a per-developer, gitignored file holding real local Postgres
credentials for running the test suite. Copy this file to `tests/env.py` and fill in the values
for your machine (see CLAUDE.md-style convention this mirrors, used across the author's other
Django projects: secrets never go in git, only their shape does).
"""

DATABASE = dict(
    ENGINE="django.db.backends.postgresql",
    NAME="django_licensing",
    USER="klubis",
    PASSWORD="changeme",
    HOST="localhost",
    PORT=5432,
    TEST=dict(NAME="test_django_licensing"),
)
