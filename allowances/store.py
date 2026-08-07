"""
The storage port.

This is the whole surface the engine needs from persistence — fourteen methods. Implement it and
the engine runs against your storage unchanged. Two implementations ship with the library:
:class:`allowances.backends.memory.MemoryStore` and ``allowances_django.store.DjangoStore``.

Two things every implementation owes the engine:

**Ordering.** Wallets come back oldest-first, by ``(created_at, id)``. The engine treats the order
it receives as the tie-breaker within one ``fulfillment_order`` step, so an unstable order means
non-deterministic draws.

**Locking.** ``unit_of_work()`` must make everything inside it atomic, and ``lock=True`` must
prevent a concurrent fulfillment from seeing the same rows until the block ends. Without both, two
simultaneous ``fulfill()`` calls can each see the same balance and overdraw it. A backend that
genuinely cannot lock should say so in its documentation rather than pretend.

A backend that cannot support an operation at all should raise
:class:`allowances.errors.UnsupportedOperationError` rather than silently doing nothing.
"""

from __future__ import annotations

import uuid

from contextlib import AbstractContextManager
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable, Sequence

from allowances.types import (
    FulfillmentRecord,
    ObjectRef,
    SubjectRef,
    Transaction,
    TransactionKind,
    Wallet,
    WalletType,
)


@runtime_checkable
class Store(Protocol):
    """Everything the engine needs from persistence."""

    # -- transactions / locking ----------------------------------------------------------------

    def unit_of_work(self) -> AbstractContextManager[None]:
        """
        Make everything inside the block atomic: it all commits, or none of it does. Must be safe
        to nest — the engine opens one, and a caller may already have opened another around it.
        """
        ...

    # -- wallets -------------------------------------------------------------------------------

    def find_wallets(
        self,
        *,
        subjects: Sequence[SubjectRef],
        wallet_types: Sequence[WalletType],
        currencies: Sequence[str],
        at: datetime,
        lock: bool = False,
    ) -> list[Wallet]:
        """
        Wallets belonging to any of ``subjects``, of any of ``wallet_types``, in any of
        ``currencies``, valid at ``at`` — ordered by ``(created_at, id)``.

        "Valid at" means ``valid_from`` is null or in the past *and* ``valid_until`` is null or in
        the future. Expired wallets are invisible to fulfillment.
        """
        ...

    def get_wallet(self, wallet_id: object, *, lock: bool = False) -> Wallet | None:
        """
        Fetch one wallet by id, or ``None`` if it is gone. Reversals need this: a transaction knows
        only which wallet it drew from, and an archived wallet is legitimately absent.
        """
        ...

    def save_wallet(self, wallet: Wallet) -> None:
        """Persist a wallet's mutable fields: ``balance`` and ``first_overdraft_at``."""
        ...

    def create_wallet(
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
    ) -> Wallet:
        """Open a new wallet and return it, with ``id`` and ``created_at`` filled in."""
        ...

    def close_wallets(self, *, subject: SubjectRef, at: datetime) -> int:
        """
        Set ``valid_until = at`` on every wallet of ``subject`` currently valid at ``at``, making
        them invisible to fulfillment from that instant. Returns how many were closed.
        """
        ...

    def find_expired_wallets(self, *, at: datetime, lock: bool = False) -> list[Wallet]:
        """Wallets whose ``valid_until`` has passed — candidates for archival."""
        ...

    def archive_wallet(self, wallet: Wallet) -> None:
        """
        Move a wallet and its transactions out of live storage into whatever archive this backend
        keeps, then delete the live rows. What the archive looks like is entirely the backend's
        business — the core only decides *when* to archive, never *how* it is stored.
        """
        ...

    # -- ledger --------------------------------------------------------------------------------

    def sum_transactions(self, *, wallet_id: object, since: datetime, until: datetime) -> Decimal:
        """
        Sum of ``amount`` over the wallet's transactions in ``[since, until]``, or zero if there
        are none. Drives sliding-window balances, so it is on the hot path of every fulfillment
        against a windowed wallet.
        """
        ...

    def create_transaction(
        self,
        *,
        wallet_id: object,
        kind: TransactionKind,
        amount: Decimal,
        record_guid: uuid.UUID | None = None,
        feature: str = "",
        storno_of_id: object = None,
        at: datetime | None = None,
    ) -> Transaction:
        """
        Append a ledger entry. ``amount`` is signed.

        A transaction may be stornoed at most once — a backend should enforce that with a
        uniqueness guarantee on ``storno_of_id`` rather than trusting callers, since a lost race
        here silently doubles a refund.
        """
        ...

    def transactions_for_record(self, *, record_guid: uuid.UUID, lock: bool = False) -> list[Transaction]:
        """
        Every transaction under one fulfillment record, ordered by ``(created_at, id)`` — storno
        rows included, so the engine can work out for itself what has already been reversed.
        """
        ...

    def attach_object(self, *, transaction_ids: Sequence[object], obj: ObjectRef) -> None:
        """Point the given transactions at the domain object they turned out to pay for."""
        ...

    # -- fulfillment records -------------------------------------------------------------------

    def get_or_create_record(self, guid: uuid.UUID | None = None) -> FulfillmentRecord:
        """
        Fetch the record with this ``guid``, creating it if absent. ``guid=None`` always creates a
        fresh one — that is how a caller says "this is its own operation".
        """
        ...

    def get_record(self, guid: uuid.UUID, *, lock: bool = False) -> FulfillmentRecord | None:
        """
        Fetch a record, or ``None``. With ``lock=True`` this serialises concurrent operations on
        the same record — which is what stops two simultaneous ``unfulfill()`` calls from each
        deciding the same transaction still needs reversing.
        """
        ...

    def records_with_unattached_transactions(self, *, older_than: datetime | None = None) -> list[FulfillmentRecord]:
        """
        Records holding at least one transaction that never got a domain object attached — a
        fulfillment whose business operation may have died halfway. Feeds recovery jobs.
        """
        ...

    # -- modifiers -----------------------------------------------------------------------------

    def find_modifier_factors(self, *, feature: str, subjects: Sequence[SubjectRef], at: datetime) -> list[Decimal]:
        """Factors of every modifier for ``feature`` that applies to any of ``subjects`` at ``at``."""
        ...
