"""
The Django ORM store for the synchronous API, :class:`DjangoStore`.

Its methods are coroutines whose bodies run the Django ORM directly on the calling thread and never suspend, so
:mod:`allowances.sync` runs the engine with it without an event loop. ``unit_of_work()`` is ``transaction.atomic``,
entered and exited synchronously.

Django refuses synchronous ORM calls on a thread that runs an event loop; asynchronous code calls the synchronous
API from a worker thread (``asyncio.to_thread``) or uses :class:`allowances_django.store.DjangoStore`.
"""

from __future__ import annotations

import functools

from contextlib import asynccontextmanager
from typing import AsyncIterator

from allowances_django.queries import DjangoQueries


def _immediate(method):
    """A coroutine method that calls the :class:`DjangoQueries` method ``method`` on the calling thread."""

    @functools.wraps(method)
    async def call(self, *args, **kwargs):
        return method(self.queries, *args, **kwargs)

    return call


class DjangoStore:
    """
    A Store over the models in ``allowances_django.models`` that never suspends.

    Besides the port it has ``resolve`` and ``live_wallet_ids``, used by the Django shortcuts.
    """

    def __init__(self, using: str | None = None):
        #: The queries, on database alias ``using`` (``None``: the default database).
        self.queries = DjangoQueries(using)

    @property
    def using(self) -> str | None:
        """Database alias of the allowances tables; ``None`` for the default database."""
        return self.queries.using

    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator[None]:
        with self.queries.atomic():
            yield

    lock_wallets = _immediate(DjangoQueries.lock_wallets)
    find_wallets = _immediate(DjangoQueries.find_wallets)
    get_wallet = _immediate(DjangoQueries.get_wallet)
    save_wallet = _immediate(DjangoQueries.save_wallet)
    create_wallet = _immediate(DjangoQueries.create_wallet)
    find_scheduled_wallets = _immediate(DjangoQueries.find_scheduled_wallets)
    find_expired_wallets = _immediate(DjangoQueries.find_expired_wallets)
    archive_wallet = _immediate(DjangoQueries.archive_wallet)
    sum_transactions = _immediate(DjangoQueries.sum_transactions)
    transactions_for_wallet = _immediate(DjangoQueries.transactions_for_wallet)
    create_transaction = _immediate(DjangoQueries.create_transaction)
    transactions_for_record = _immediate(DjangoQueries.transactions_for_record)
    attach_object = _immediate(DjangoQueries.attach_object)
    get_or_create_record = _immediate(DjangoQueries.get_or_create_record)
    get_record = _immediate(DjangoQueries.get_record)
    records_with_unattached_transactions = _immediate(DjangoQueries.records_with_unattached_transactions)
    find_modifier_factors = _immediate(DjangoQueries.find_modifier_factors)
    add_membership = _immediate(DjangoQueries.add_membership)
    remove_membership = _immediate(DjangoQueries.remove_membership)
    memberships_of = _immediate(DjangoQueries.memberships_of)
    lock_memberships = _immediate(DjangoQueries.lock_memberships)
    resolve = _immediate(DjangoQueries.resolve)
    live_wallet_ids = _immediate(DjangoQueries.live_wallet_ids)
