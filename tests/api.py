"""
The engine API as the tests call it: one coroutine per public function of :mod:`allowances`, with the same
signature.

For every store but one it awaits the function of :mod:`allowances`. A :class:`ThreadedSyncStore` (the
``django-sync`` store) runs the twin of :mod:`allowances.sync` on the worker thread of that store, so the
synchronous Django ORM never runs on the event loop.
"""

from __future__ import annotations

import asyncio
import functools

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import allowances
import allowances.sync

from allowances.sync import driver


class ThreadedSyncStore:
    """
    A store that never suspends, driven through :mod:`allowances.sync` on one worker thread of its own.

    Store methods are coroutines that run the wrapped store's method on that thread. ``unit_of_work()`` enters and
    leaves the wrapped store's unit of work there, so every call inside the block joins it.
    """

    def __init__(self, store):
        self.store = store
        self._executor = ThreadPoolExecutor(max_workers=1)

    async def call(self, function, /, *args, **kwargs):
        """Run the synchronous ``function(*args, **kwargs)`` on the worker thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(function, *args, **kwargs))

    def __getattr__(self, name):
        method = getattr(self.store, name)
        if not callable(method):
            return method

        async def call(*args, **kwargs):
            return await self.call(lambda: driver.run(method(*args, **kwargs)))

        return call

    @asynccontextmanager
    async def unit_of_work(self):
        manager = driver.unit_of_work(self.store)
        await self.call(manager.__enter__)
        try:
            yield
        except BaseException as exc:
            if not await self.call(manager.__exit__, type(exc), exc, exc.__traceback__):
                raise
        else:
            await self.call(manager.__exit__, None, None, None)

    def close(self):
        """Close the worker thread's database connections and stop the thread."""
        from django.db import connections

        self._executor.submit(connections.close_all).result()
        self._executor.shutdown()


def raw(store):
    """The store the engine works on: the wrapped store of a :class:`ThreadedSyncStore`, else ``store``."""
    return store.store if isinstance(store, ThreadedSyncStore) else store


def _twin(name: str):
    asynchronous = getattr(allowances, name)
    synchronous = getattr(allowances.sync, name)

    @functools.wraps(asynchronous)
    async def call(store, *args, **kwargs):
        if isinstance(store, ThreadedSyncStore):
            return await store.call(synchronous, store.store, *args, **kwargs)
        return await asynchronous(store, *args, **kwargs)

    return call


add_member = _twin("add_member")
adjust_wallet = _twin("adjust_wallet")
archive_expired_wallets = _twin("archive_expired_wallets")
attach = _twin("attach")
cancel_scheduled_switch = _twin("cancel_scheduled_switch")
close_wallet = _twin("close_wallet")
consumed = _twin("consumed")
credit = _twin("credit")
effective_allowances = _twin("effective_allowances")
effective_balance = _twin("effective_balance")
find_unattached_fulfillment_records = _twin("find_unattached_fulfillment_records")
fulfill = _twin("fulfill")
groups_of = _twin("groups_of")
open_wallet = _twin("open_wallet")
peek = _twin("peek")
plan_switch = _twin("plan_switch")
remove_member = _twin("remove_member")
transactions_for_object = _twin("transactions_for_object")
transfer = _twin("transfer")
unfulfill = _twin("unfulfill")
