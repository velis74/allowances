"""
Deployment checks specific to this app.

There is exactly one, and it exists because SQLite is supported but has a single configuration
switch that decides whether concurrent fulfillment waits or raises.
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
    SQLite has no ``SELECT ... FOR UPDATE``; it serialises writers instead. A ``DEFERRED``
    transaction — Django's default — only takes that write lock when it first writes, by which
    point it may have read a balance another transaction has already changed underneath it.

    SQLite catches that itself and fails the write rather than committing a stale balance, so
    the ledger stays correct either way. But the failure surfaces as ``OperationalError:
    database is locked`` in the middle of a ``fulfill``, which is not a thing to discover in
    production. ``IMMEDIATE`` takes the lock up front, so the second caller waits its turn.
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
