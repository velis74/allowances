"""
The Django ORM store for the asynchronous API, :class:`DjangoStore`.

Django has no asynchronous form of ``transaction.atomic`` or ``select_for_update``, and a Django connection
belongs to one thread. The store therefore runs its queries (:class:`~allowances_django.queries.DjangoQueries`)
on threads:

- Outside a unit of work a call runs like Django's asynchronous ORM methods (``aget``, ``afirst`` ...):
  through ``sync_to_async`` on Django's thread-sensitive executor, in autocommit.
- The outermost ``async with store.unit_of_work()`` starts a thread of its own. ``atomic()`` is entered and
  exited on it, every store call made inside the block (from any ``DjangoStore`` instance, nested blocks
  included) runs on it, and the thread's connections are closed when the block ends. Concurrent units of work
  in one process run on separate threads, with separate connections and transactions.
- Tasks created inside a unit of work (``asyncio.gather``, ``create_task``) share its transaction. Their nested
  blocks are savepoints that run one after another; while one is open, operations and blocks of other tasks wait
  until it ends.

Each unit of work opens a database connection; a connection pool (Django's ``"pool"`` option for psycopg)
keeps that cheap. Units of work run as parallel as threads allow; the store does not do asynchronous I/O.
Asynchronous I/O is the adorm store, ``allowances_adorm.store.AdormStore``.

The store needs a running event loop. :mod:`allowances.sync` uses
:class:`allowances_django.sync.store.DjangoStore`.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools

from concurrent.futures import ThreadPoolExecutor

from asgiref.sync import sync_to_async
from django.db import connections

from allowances.errors import StoreSuspendedError
from allowances_django.queries import (  # noqa: F401  (re-exported row converters)
    DjangoQueries,
    MEMBERSHIP_LOCK_NAME,
    to_record,
    to_transaction,
    to_wallet,
)


class _UnitOfWorkThread:
    """
    The thread that runs one outermost unit of work and every store call inside it.

    Tasks created inside the unit of work share it. ``top`` is the innermost open block; only operations and
    blocks of that block's context run, others wait until it ends, so savepoints of concurrent tasks never
    interleave.
    """

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="allowances-unit-of-work")
        self.closed = False
        self.top = _Block(self, None)
        self._top_changed = asyncio.Event()

    async def call(self, function, *args, **kwargs):
        return await sync_to_async(function, thread_sensitive=False, executor=self.executor)(*args, **kwargs)

    async def wait_turn(self, block: _Block) -> None:
        """Wait until ``block`` is the innermost open block."""
        while self.top is not block:
            await self._top_changed.wait()

    async def push(self, parent: _Block) -> _Block:
        """Open a block nested in ``parent`` once ``parent`` is the innermost open block."""
        await self.wait_turn(parent)
        self.top = _Block(self, parent)
        return self.top

    def pop(self, block: _Block) -> None:
        """Close ``block`` and wake the operations waiting for its parent."""
        self.top = block.parent
        changed, self._top_changed = self._top_changed, asyncio.Event()
        changed.set()

    def close(self):
        """Close the thread's connections after the queued work, then let the thread end."""
        self.closed = True
        future = self.executor.submit(connections.close_all)
        self.executor.shutdown(wait=False)
        return future


class _Block:
    """One open ``unit_of_work`` block; ``parent`` is ``None`` for the outermost one."""

    __slots__ = ("thread", "parent")

    def __init__(self, thread: _UnitOfWorkThread, parent: _Block | None):
        self.thread = thread
        self.parent = parent


#: The unit-of-work block the current task is in, if any; tasks created inside a block inherit it.
_current_block: contextvars.ContextVar[_Block | None] = contextvars.ContextVar(
    "allowances_django_unit_of_work_block", default=None
)


def _require_event_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        raise StoreSuspendedError(
            "allowances_django.store.DjangoStore runs its queries on threads and needs a running event loop; "
            "use allowances_django.sync.store.DjangoStore with allowances.sync."
        ) from None


async def _execute(function, *args, **kwargs):
    """Run ``function`` on the current unit of work's thread, or through Django's ``sync_to_async``."""
    _require_event_loop()
    block = _current_block.get()
    if block is None or block.thread.closed:
        return await sync_to_async(function)(*args, **kwargs)
    await block.thread.wait_turn(block)
    return await block.thread.call(function, *args, **kwargs)


class _UnitOfWork:
    """``atomic()`` on the unit of work's thread; the outermost block owns the thread."""

    def __init__(self, queries: DjangoQueries):
        self._queries = queries
        self._token = None

    async def __aenter__(self) -> None:
        _require_event_loop()
        enclosing = _current_block.get()
        if enclosing is None or enclosing.thread.closed:
            self._block = _UnitOfWorkThread().top
        else:
            self._block = await enclosing.thread.push(enclosing)
        self._token = _current_block.set(self._block)
        self._atomic = self._queries.atomic()
        try:
            await self._block.thread.call(self._atomic.__enter__)
        except BaseException:
            # Not awaited: the block may be abandoned (cancelled, or closed by allowances.sync). Closing the
            # connection after a pending __enter__ rolls its transaction back.
            self._release()
            raise

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        thread = self._block.thread
        try:
            await thread.wait_turn(self._block)
            return bool(await thread.call(self._atomic.__exit__, exc_type, exc, traceback))
        finally:
            closing = self._release()
            if closing is not None:
                await asyncio.wrap_future(closing)

    def _release(self):
        """Leave the block: the outermost one closes the thread (returns its future), a nested one is popped."""
        _current_block.reset(self._token)
        thread = self._block.thread
        if self._block.parent is None:
            return thread.close()
        thread.pop(self._block)
        return None


def _on_thread(method):
    """A coroutine method that runs the :class:`DjangoQueries` method ``method`` with :func:`_execute`."""

    @functools.wraps(method)
    async def call(self, *args, **kwargs):
        return await _execute(method, self.queries, *args, **kwargs)

    return call


class DjangoStore:
    """
    A Store over the models in ``allowances_django.models`` for the asynchronous API; see the module
    documentation for the execution model.

    Besides the port it has ``resolve`` and ``live_wallet_ids``, used by the Django shortcuts.
    """

    def __init__(self, using: str | None = None):
        #: The queries, on database alias ``using`` (``None``: the default database).
        self.queries = DjangoQueries(using)

    @property
    def using(self) -> str | None:
        """Database alias of the allowances tables; ``None`` for the default database."""
        return self.queries.using

    def unit_of_work(self) -> _UnitOfWork:
        return _UnitOfWork(self.queries)

    lock_wallets = _on_thread(DjangoQueries.lock_wallets)
    find_wallets = _on_thread(DjangoQueries.find_wallets)
    get_wallet = _on_thread(DjangoQueries.get_wallet)
    save_wallet = _on_thread(DjangoQueries.save_wallet)
    create_wallet = _on_thread(DjangoQueries.create_wallet)
    find_scheduled_wallets = _on_thread(DjangoQueries.find_scheduled_wallets)
    find_expired_wallets = _on_thread(DjangoQueries.find_expired_wallets)
    archive_wallet = _on_thread(DjangoQueries.archive_wallet)
    sum_transactions = _on_thread(DjangoQueries.sum_transactions)
    transactions_for_wallet = _on_thread(DjangoQueries.transactions_for_wallet)
    create_transaction = _on_thread(DjangoQueries.create_transaction)
    transactions_for_record = _on_thread(DjangoQueries.transactions_for_record)
    attach_object = _on_thread(DjangoQueries.attach_object)
    get_or_create_record = _on_thread(DjangoQueries.get_or_create_record)
    get_record = _on_thread(DjangoQueries.get_record)
    records_with_unattached_transactions = _on_thread(DjangoQueries.records_with_unattached_transactions)
    find_modifier_factors = _on_thread(DjangoQueries.find_modifier_factors)
    add_membership = _on_thread(DjangoQueries.add_membership)
    remove_membership = _on_thread(DjangoQueries.remove_membership)
    memberships_of = _on_thread(DjangoQueries.memberships_of)
    lock_memberships = _on_thread(DjangoQueries.lock_memberships)
    resolve = _on_thread(DjangoQueries.resolve)
    live_wallet_ids = _on_thread(DjangoQueries.live_wallet_ids)
