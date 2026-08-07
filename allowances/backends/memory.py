"""
An in-memory :class:`~allowances.store.Store`.

Two jobs. It makes the test suite fast — the engine's behaviour is arithmetic, and proving it
doesn't need a database. And it keeps the port honest: every time the engine reaches for something
only an ORM could do, this backend stops compiling, which is the signal that the abstraction leaked.

Not for production. State lives in a dict, so it dies with the process and is invisible to every
other one. Locking is a process-local ``RLock``, which is exactly as much concurrency control as a
single-process test needs and no more.
"""

from __future__ import annotations

import itertools
import threading
import uuid

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from allowances.types import (
    FulfillmentRecord,
    Modifier,
    ObjectRef,
    SubjectRef,
    Transaction,
    TransactionKind,
    Wallet,
    WalletType,
)
from allowances.windows import utcnow


class MemoryStore:
    """A complete Store implementation over plain dicts."""

    def __init__(self):
        self._wallets: dict[int, Wallet] = {}
        self._transactions: dict[int, Transaction] = {}
        self._records: dict[uuid.UUID, FulfillmentRecord] = {}
        self._modifiers: list[Modifier] = []
        self.archived_wallets: list[Wallet] = []
        self.archived_transactions: list[Transaction] = []

        self._wallet_ids = itertools.count(1)
        self._transaction_ids = itertools.count(1)
        self._modifier_ids = itertools.count(1)
        self._lock = threading.RLock()
        self._depth = 0
        # Distinguishes rows created within the same microsecond, so ordering stays deterministic
        # on clocks too coarse to separate them.
        self._sequence = itertools.count(1)
        self._created_seq: dict[tuple[str, int], int] = {}

    # -- transactions / locking ----------------------------------------------------------------

    @contextmanager
    def unit_of_work(self):
        """
        Atomic and re-entrant. The outermost block snapshots every collection and restores it if
        anything raises; nested blocks join the outer one, exactly as a nested ``atomic()`` does.
        """
        self._lock.acquire()
        self._depth += 1
        snapshot = self._snapshot() if self._depth == 1 else None
        try:
            yield
        except BaseException:
            if snapshot is not None:
                self._restore(snapshot)
            raise
        finally:
            self._depth -= 1
            self._lock.release()

    def _snapshot(self):
        # Every stored value is a frozen dataclass, so copying the containers is enough.
        return (
            dict(self._wallets),
            dict(self._transactions),
            dict(self._records),
            list(self._modifiers),
            list(self.archived_wallets),
            list(self.archived_transactions),
            dict(self._created_seq),
        )

    def _restore(self, snapshot):
        (
            self._wallets,
            self._transactions,
            self._records,
            self._modifiers,
            self.archived_wallets,
            self.archived_transactions,
            self._created_seq,
        ) = snapshot

    def _sort_key(self, kind: str, obj):
        return (obj.created_at, self._created_seq.get((kind, obj.id), 0), obj.id)

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
        subject_set = set(subjects)
        type_set = {str(wallet_type) for wallet_type in wallet_types}
        currency_set = set(currencies)
        found = [
            wallet
            for wallet in self._wallets.values()
            if wallet.subject in subject_set
            and str(wallet.wallet_type) in type_set
            and wallet.currency in currency_set
            and wallet.is_valid_at(at)
        ]
        return sorted(found, key=lambda w: self._sort_key("wallet", w))

    def get_wallet(self, wallet_id: object, *, lock: bool = False) -> Wallet | None:
        return self._wallets.get(wallet_id)

    def save_wallet(self, wallet: Wallet) -> None:
        if wallet.id in self._wallets:
            self._wallets[wallet.id] = wallet

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
        wallet = Wallet(
            id=next(self._wallet_ids),
            subject=subject,
            wallet_type=WalletType(wallet_type),
            currency=currency,
            balance=Decimal(str(balance)),
            created_at=utcnow(),
            valid_from=valid_from,
            valid_until=valid_until,
            overdraft_limit=None if overdraft_limit is None else Decimal(str(overdraft_limit)),
            reset_config=reset_config,
        )
        self._wallets[wallet.id] = wallet
        self._created_seq[("wallet", wallet.id)] = next(self._sequence)
        return wallet

    def close_wallets(self, *, subject: SubjectRef, at: datetime) -> int:
        closed = 0
        for wallet_id, wallet in list(self._wallets.items()):
            if wallet.subject == subject and wallet.is_valid_at(at):
                self._wallets[wallet_id] = replace(wallet, valid_until=at)
                closed += 1
        return closed

    def find_expired_wallets(self, *, at: datetime, lock: bool = False) -> list[Wallet]:
        expired = [w for w in self._wallets.values() if w.valid_until is not None and w.valid_until <= at]
        return sorted(expired, key=lambda w: self._sort_key("wallet", w))

    def archive_wallet(self, wallet: Wallet) -> None:
        stored = self._wallets.pop(wallet.id, None)
        if stored is None:
            return
        self.archived_wallets.append(wallet)
        for txn_id, txn in list(self._transactions.items()):
            if txn.wallet_id == wallet.id:
                self.archived_transactions.append(txn)
                del self._transactions[txn_id]

    # -- ledger --------------------------------------------------------------------------------

    def sum_transactions(self, *, wallet_id: object, since: datetime, until: datetime) -> Decimal:
        return sum(
            (
                t.amount
                for t in self._transactions.values()
                if t.wallet_id == wallet_id and since <= t.created_at <= until
            ),
            Decimal("0"),
        )

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
        if storno_of_id is not None and any(t.storno_of_id == storno_of_id for t in self._transactions.values()):
            # The relational backends enforce this with a unique constraint; a lost race here would
            # silently refund twice, so the in-memory one refuses just as loudly.
            raise ValueError(f"Transaction {storno_of_id!r} has already been stornoed")
        txn = Transaction(
            id=next(self._transaction_ids),
            wallet_id=wallet_id,
            kind=TransactionKind(kind),
            amount=Decimal(str(amount)),
            created_at=at or utcnow(),
            record_guid=record_guid,
            feature=feature,
            storno_of_id=storno_of_id,
        )
        self._transactions[txn.id] = txn
        self._created_seq[("transaction", txn.id)] = next(self._sequence)
        return txn

    def transactions_for_record(self, *, record_guid: uuid.UUID, lock: bool = False) -> list[Transaction]:
        found = [t for t in self._transactions.values() if t.record_guid == record_guid]
        return sorted(found, key=lambda t: self._sort_key("transaction", t))

    def attach_object(self, *, transaction_ids: Sequence[object], obj: ObjectRef) -> None:
        for txn_id in transaction_ids:
            txn = self._transactions.get(txn_id)
            if txn is not None:
                self._transactions[txn_id] = replace(txn, obj=obj)

    # -- fulfillment records -------------------------------------------------------------------

    def get_or_create_record(self, guid: uuid.UUID | None = None) -> FulfillmentRecord:
        if guid is None:
            guid = uuid.uuid4()
        record = self._records.get(guid)
        if record is None:
            record = FulfillmentRecord(guid=guid, created_at=utcnow())
            self._records[guid] = record
        return record

    def get_record(self, guid: uuid.UUID, *, lock: bool = False) -> FulfillmentRecord | None:
        return self._records.get(guid)

    def records_with_unattached_transactions(self, *, older_than: datetime | None = None) -> list[FulfillmentRecord]:
        guids = {t.record_guid for t in self._transactions.values() if t.obj is None and t.record_guid is not None}
        records = [self._records[g] for g in guids if g in self._records]
        if older_than is not None:
            records = [r for r in records if r.created_at <= older_than]
        return sorted(records, key=lambda r: (r.created_at, str(r.guid)))

    # -- modifiers -----------------------------------------------------------------------------

    def add_modifier(
        self,
        *,
        subject: SubjectRef,
        feature: str,
        factor: Decimal,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> Modifier:
        """Not part of the port — modifiers are seeded by an application, not by the engine."""
        modifier = Modifier(
            id=next(self._modifier_ids),
            subject=subject,
            feature=feature,
            factor=Decimal(str(factor)),
            valid_from=valid_from,
            valid_until=valid_until,
        )
        self._modifiers.append(modifier)
        return modifier

    def find_modifier_factors(self, *, feature: str, subjects: Sequence[SubjectRef], at: datetime) -> list[Decimal]:
        subject_set = set(subjects)
        return [
            m.factor
            for m in self._modifiers
            if m.feature == feature
            and m.subject in subject_set
            and (m.valid_from is None or m.valid_from <= at)
            and (m.valid_until is None or m.valid_until >= at)
        ]
