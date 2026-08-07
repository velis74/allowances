"""
Copy to ``tests/env.py`` (gitignored) and fill in local Postgres credentials.

Postgres rather than SQLite because the suite exercises ``select_for_update`` and partial unique
constraints, neither of which SQLite implements the way the library relies on.
"""

DATABASE = dict(
    ENGINE="django.db.backends.postgresql",
    NAME="allowances",
    USER="postgres",
    PASSWORD="",
    HOST="localhost",
    PORT=5432,
    TEST=dict(NAME="test_allowances"),
)
