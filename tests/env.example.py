"""
Copy to ``tests/env.py`` (gitignored) to run the suite against something other than SQLite.

Without this file the suite uses a file-backed SQLite database with the settings the library
requires of SQLite deployments: IMMEDIATE transactions, WAL and a busy timeout.

CI runs the suite on both engines. Use a dedicated database server or user: Django creates and
drops the test database (``TEST.NAME``) on it.
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
