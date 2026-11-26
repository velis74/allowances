"""
The storage port.

The persistence interface of the engine. Implementations shipped with the library:
:class:`allowances.backends.memory.MemoryStore`, ``allowances_django.store.DjangoStore`` (Django ORM, for the
asynchronous API), ``allowances_django.sync.store.DjangoStore`` (Django ORM, never suspends) and
``allowances_adorm.store.AdormStore`` (adorm).

Every implementation guarantees:

**Ordering.** Wallets are returned oldest first, by ``(created_at, id)``. The engine uses this order
as the tie-breaker within one ``fulfillment_order`` step; an unstable order makes draws
non-deterministic.

**Locking.** ``unit_of_work()`` must make everything inside it atomic, and a locked read must
prevent a concurrent operation from changing the same rows until the block ends. Without both, two
concurrent ``fulfill()`` calls can read the same balance and overdraw it. A backend that cannot
lock states this limitation in its documentation.

**Lock order.** An engine operation that locks wallets locks all of them in one
:meth:`Store.lock_wallets` call, before reading the balances it relies on, and locks no further
wallet afterwards. ``lock_wallets`` takes the locks in ascending wallet id, so two operations never
wait for each other in a cycle. Wallet ids of one store must therefore be mutually comparable.
A fulfilment record is locked before its wallets.

**Execution.** Every method except ``unit_of_work`` is a coroutine function; ``unit_of_work`` returns an
asynchronous context manager. A store whose methods and context manager never suspend (their bodies await
nothing that yields to the event loop) also serves the synchronous API, :mod:`allowances.sync`, which runs
the engine's coroutines without an event loop. A store that suspends works only with the asynchronous API.

A backend raises :class:`allowances.errors.UnsupportedOperationError` for an operation it does not
support.

**Optional methods.** A store may also implement the methods below. They are not members of the
:class:`Store` protocol, so ``isinstance(store, Store)`` does not depend on them. The engine looks each one
up on the store; the function that needs a missing one raises
:class:`~allowances.errors.UnsupportedOperationError`, every other function works.

``async def transactions_for_object(self, *, obj: ObjectRef) -> list[Transaction]``
    Every live transaction whose ``obj`` equals ``obj``, ordered by ``(created_at, id)``. Used by
    :func:`allowances.engine.transactions_for_object`.
"""

from __future__ import annotations

import uuid

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable, Sequence

from allowances.types import (
    FulfillmentRecord,
    ObjectRef,
    PLAN_SOURCE,
    SubjectRef,
    Transaction,
    TransactionKind,
    Wallet,
    WalletType,
)


@runtime_checkable
class Store(Protocol):
    """The persistence operations the engine uses."""

    # -- transactions / locking ----------------------------------------------------------------

    def unit_of_work(self) -> AbstractAsyncContextManager[None]:
        """
        An asynchronous context manager (``async with store.unit_of_work():``) that makes everything
        inside the block atomic: it all commits, or none of it does. Blocks nest: the engine opens one,
        possibly inside a block the caller opened. A nested block that raises discards its own writes and
        leaves the outer block's intact.
        """
        ...

    # -- wallets -------------------------------------------------------------------------------

    async def find_wallets(
        self,
        *,
        subjects: Sequence[SubjectRef],
        wallet_types: Sequence[WalletType],
        currencies: Sequence[str] | None,
        at: datetime,
        lock: bool = False,
    ) -> list[Wallet]:
        """
        Wallets belonging to any of ``subjects``, of any of ``wallet_types``, in any of
        ``currencies`` (``None``: every currency), valid at ``at`` — ordered by ``(created_at, id)``.

        "Valid at" means ``valid_from`` is null or ``<= at``, and ``valid_until`` is null or ``> at``.
        """
        ...

    async def lock_wallets(self, wallet_ids: Sequence[object]) -> list[Wallet]:
        """
        Lock the given live wallets in ascending id order and return their current state in that
        order. Ids of archived or unknown wallets are omitted; a duplicated id is returned once.
        """
        ...

    async def get_wallet(self, wallet_id: object, *, lock: bool = False) -> Wallet | None:
        """
        Fetch one wallet by id; ``None`` for an archived or unknown wallet.
        """
        ...

    async def save_wallet(self, wallet: Wallet) -> None:
        """
        Persist a wallet's mutable fields: ``balance``, ``first_overdraft_at``, ``valid_from``,
        ``valid_until``, ``overdraft_limit``, ``reset_config``, ``allotment`` and ``scheduled``.
        """
        ...

    async def create_wallet(
        self,
        *,
        subject: SubjectRef,
        wallet_type: WalletType,
        currency: str,
        balance: Decimal,
        overdraft_limit: Decimal | None = None,
        reset_config: dict | None = None,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        source: str = PLAN_SOURCE,
        allotment: Decimal | None = None,
        scheduled: dict | None = None,
    ) -> Wallet:
        """Open a new wallet and return it, with ``id`` and ``created_at`` filled in."""
        ...

    async def find_scheduled_wallets(self, *, subject: SubjectRef, lock: bool = False) -> list[Wallet]:
        """
        Every wallet of ``subject`` whose ``scheduled`` is set, valid or not, ordered by
        ``(created_at, id)``.
        """
        ...

    async def find_expired_wallets(self, *, at: datetime, lock: bool = False) -> list[Wallet]:
        """Wallets whose ``valid_until`` is set and ``<= at``, ordered by ``(created_at, id)``."""
        ...

    async def archive_wallet(self, wallet: Wallet) -> None:
        """
        Move a wallet and its transactions to the backend's archive and delete the live rows. The
        archive format is backend-specific.
        """
        ...

    # -- ledger --------------------------------------------------------------------------------

    async def sum_transactions(self, *, wallet_id: object, since: datetime, until: datetime) -> Decimal:
        """
        Sum of ``amount`` over the wallet's transactions in ``[since, until]``, or zero if there
        are none. Runs on every fulfilment against a windowed wallet; implementations answer it with
        an indexed query.
        """
        ...

    async def transactions_for_wallet(
        self, *, wallet_id: object, since: datetime, until: datetime
    ) -> list[Transaction]:
        """The wallet's transactions in ``[since, until]``, ordered by ``(created_at, id)``."""
        ...

    async def create_transaction(
        self,
        *,
        wallet_id: object,
        kind: TransactionKind,
        amount: Decimal,
        record_guid: uuid.UUID | None = None,
        feature: str = "",
        storno_of_id: object = None,
        at: datetime | None = None,
        actor: SubjectRef | None = None,
        metadata: dict | None = None,
    ) -> Transaction:
        """
        Append a ledger entry. ``amount`` is signed; ``actor`` is the subject on whose behalf the
        entry is made.

        ``metadata`` is the application's JSON-serialisable data for the entry. The store persists it
        unchanged and returns it on every :class:`~allowances.types.Transaction` it reads.

        A transaction is stornoed at most once; the backend enforces this with a uniqueness
        constraint on ``storno_of_id``. Without it, two concurrent reversals refund twice.
        """
        ...

    async def transactions_for_record(self, *, record_guid: uuid.UUID, lock: bool = False) -> list[Transaction]:
        """
        Every transaction under one fulfilment record, storno rows included, ordered by
        ``(created_at, id)``.
        """
        ...

    async def attach_object(self, *, transaction_ids: Sequence[object], obj: ObjectRef) -> None:
        """Set ``obj`` on the given transactions."""
        ...

    # -- fulfilment records -------------------------------------------------------------------

    async def get_or_create_record(self, guid: uuid.UUID | None = None) -> FulfillmentRecord:
        """
        Fetch the record with this ``guid``, creating it if absent. ``guid=None`` always creates a
        new record with a new guid.
        """
        ...

    async def get_record(self, guid: uuid.UUID, *, lock: bool = False) -> FulfillmentRecord | None:
        """
        Fetch a record, or ``None``. ``lock=True`` serialises concurrent operations on the record,
        so two concurrent ``unfulfill()`` calls cannot reverse the same transaction twice.
        """
        ...

    async def records_with_unattached_transactions(
        self, *, older_than: datetime | None = None
    ) -> list[FulfillmentRecord]:
        """
        Records holding at least one ``CONSUMPTION`` transaction without an attached domain object
        (the business operation may have failed before ``attach()``). Transactions of other kinds
        (grants, transfers) do not make a record match.
        """
        ...

    # -- memberships ---------------------------------------------------------------------------

    async def add_membership(self, *, member: SubjectRef, group: SubjectRef) -> None:
        """Record that ``member`` is a direct member of ``group``. The pair is stored at most once."""
        ...

    async def remove_membership(self, *, member: SubjectRef, group: SubjectRef) -> bool:
        """Remove the direct membership of ``member`` in ``group``. Returns whether it existed."""
        ...

    async def memberships_of(self, *, members: Sequence[SubjectRef]) -> list[tuple[SubjectRef, SubjectRef]]:
        """
        Direct ``(member, group)`` pairs whose member is any of ``members``, ordered by creation,
        oldest first.
        """
        ...

    async def lock_memberships(self) -> None:
        """
        Serialise membership changes for the rest of the current unit of work, on every database
        the backend supports.
        """
        ...

    # -- modifiers -----------------------------------------------------------------------------

    async def find_modifier_factors(
        self, *, feature: str, subjects: Sequence[SubjectRef], at: datetime
    ) -> list[Decimal]:
        """
        Factors of every modifier for ``feature`` that applies to any of ``subjects`` at ``at``: ``valid_from``
        null or ``<= at``, ``valid_until`` null or ``> at``.
        """
        ...
