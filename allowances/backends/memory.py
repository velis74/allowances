"""
An in-memory :class:`~allowances.store.Store`.

Used by the test suite, and a reference implementation of the Store protocol without an ORM or
database.

Not for production: state is held in process memory, lost when the process exits and not shared
between processes. Locking is one process-local ``RLock`` held by ``unit_of_work``; ``lock=``
arguments are ignored.
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
    PLAN_SOURCE,
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
        self._memberships: list[tuple[SubjectRef, SubjectRef]] = []
        self.archived_wallets: list[Wallet] = []
        self.archived_transactions: list[Transaction] = []

        self._wallet_ids = itertools.count(1)
        self._transaction_ids = itertools.count(1)
        self._modifier_ids = itertools.count(1)
        self._lock = threading.RLock()
        # Orders rows created within the same clock tick.
        self._sequence = itertools.count(1)
        self._created_seq: dict[tuple[str, int], int] = {}

    # -- transactions / locking ----------------------------------------------------------------

    @contextmanager
    def unit_of_work(self):
        """
        Atomic and re-entrant. Each block snapshots every collection and restores it if the block
        raises; a nested block that raises discards only its own writes, as a nested ``atomic()``
        savepoint does.
        """
        self._lock.acquire()
        snapshot = self._snapshot()
        try:
            yield
        except BaseException:
            self._restore(snapshot)
            raise
        finally:
            self._lock.release()

    def _snapshot(self):
        # Every stored value is a frozen dataclass, so copying the containers is enough.
        return (
            dict(self._wallets),
            dict(self._transactions),
            dict(self._records),
            list(self._modifiers),
            list(self._memberships),
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
            self._memberships,
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
        currencies: Sequence[str] | None,
        at: datetime,
        lock: bool = False,
    ) -> list[Wallet]:
        subject_set = set(subjects)
        type_set = {str(wallet_type) for wallet_type in wallet_types}
        currency_set = None if currencies is None else set(currencies)
        found = [
            wallet
            for wallet in self._wallets.values()
            if wallet.subject in subject_set
            and str(wallet.wallet_type) in type_set
            and (currency_set is None or wallet.currency in currency_set)
            and wallet.is_valid_at(at)
        ]
        return sorted(found, key=lambda w: self._sort_key("wallet", w))

    def lock_wallets(self, wallet_ids: Sequence[object]) -> list[Wallet]:
        return [self._wallets[wallet_id] for wallet_id in sorted(set(wallet_ids)) if wallet_id in self._wallets]

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
        source: str = PLAN_SOURCE,
        allotment: Decimal | None = None,
        scheduled: dict | None = None,
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
            source=source,
            allotment=None if allotment is None else Decimal(str(allotment)),
            scheduled=scheduled,
        )
        self._wallets[wallet.id] = wallet
        self._created_seq[("wallet", wallet.id)] = next(self._sequence)
        return wallet

    def find_scheduled_wallets(self, *, subject: SubjectRef, lock: bool = False) -> list[Wallet]:
        found = [w for w in self._wallets.values() if w.subject == subject and w.scheduled]
        return sorted(found, key=lambda w: self._sort_key("wallet", w))

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

    def transactions_for_wallet(self, *, wallet_id: object, since: datetime, until: datetime) -> list[Transaction]:
        found = [t for t in self._transactions.values() if t.wallet_id == wallet_id and since <= t.created_at <= until]
        return sorted(found, key=lambda t: self._sort_key("transaction", t))

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
        actor: SubjectRef | None = None,
    ) -> Transaction:
        if storno_of_id is not None and any(t.storno_of_id == storno_of_id for t in self._transactions.values()):
            # A transaction may be stornoed at most once.
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
            actor=actor,
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

    # -- fulfilment records -------------------------------------------------------------------

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
        guids = {
            t.record_guid
            for t in self._transactions.values()
            if t.kind == TransactionKind.CONSUMPTION and t.obj is None and t.record_guid is not None
        }
        records = [self._records[g] for g in guids if g in self._records]
        if older_than is not None:
            records = [r for r in records if r.created_at <= older_than]
        return sorted(records, key=lambda r: (r.created_at, str(r.guid)))

    # -- memberships ---------------------------------------------------------------------------

    def add_membership(self, *, member: SubjectRef, group: SubjectRef) -> None:
        if (member, group) not in self._memberships:
            self._memberships.append((member, group))

    def remove_membership(self, *, member: SubjectRef, group: SubjectRef) -> bool:
        if (member, group) in self._memberships:
            self._memberships.remove((member, group))
            return True
        return False

    def memberships_of(self, *, members: Sequence[SubjectRef]) -> list[tuple[SubjectRef, SubjectRef]]:
        member_set = set(members)
        return [pair for pair in self._memberships if pair[0] in member_set]

    def lock_memberships(self) -> None:
        """A no-op: ``unit_of_work`` already holds the store-wide lock."""

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
        """Add a modifier. Not part of the Store protocol."""
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
