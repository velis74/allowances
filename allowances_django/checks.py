"""
Deployment checks of the Django app.

``allowances.W001``: an SQLite database whose ``transaction_mode`` is not ``IMMEDIATE`` or ``EXCLUSIVE``.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Warning as CheckWarning

#: Modes that make ``atomic()`` take SQLite's write lock at ``BEGIN`` rather than on first write.
SAFE_TRANSACTION_MODES = {"IMMEDIATE", "EXCLUSIVE"}

HINT = (
    'DATABASES["{alias}"]["OPTIONS"] = {{"transaction_mode": "IMMEDIATE", "timeout": 20, '
    '"init_command": "PRAGMA journal_mode=WAL;"}}'
)


def check_sqlite_transaction_mode(app_configs, **kwargs):
    """
    Warn for each SQLite database whose ``transaction_mode`` is not ``IMMEDIATE`` or ``EXCLUSIVE``.

    SQLite has no ``SELECT ... FOR UPDATE`` and serialises writers. A ``DEFERRED`` transaction
    (Django's default) takes the write lock on its first write, after it may have read a balance that
    another transaction has changed. SQLite then fails the write with ``OperationalError: database is
    locked`` inside ``fulfill``; no stale balance is committed. ``IMMEDIATE`` takes the lock at
    ``BEGIN``, so a concurrent ``fulfill`` waits for the lock.
    """
    problems = []
    for alias, config in settings.DATABASES.items():
        if not config.get("ENGINE", "").endswith("sqlite3"):
            continue
        mode = (config.get("OPTIONS") or {}).get("transaction_mode") or ""
        if mode.upper() in SAFE_TRANSACTION_MODES:
            continue
        problems.append(
            CheckWarning(
                f'The SQLite database "{alias}" does not use IMMEDIATE transactions, so concurrent '
                f"fulfillment can raise OperationalError instead of waiting for its turn.",
                hint=HINT.format(alias=alias),
                id="allowances.W001",
            )
        )
    return problems
