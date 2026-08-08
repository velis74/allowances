"""
Copy to ``tests/env.py`` (gitignored) to run the suite against something other than SQLite.

You do not need this file to run the tests. Without it the suite uses a file-backed SQLite
database configured exactly as the library asks deployments to configure one — IMMEDIATE
transactions, WAL, a real busy timeout — and passes in full.

Both engines are supported and CI runs both, so covering Postgres locally is still worth the
five minutes. Point this at a database of its own, never at a host database you care about.
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
