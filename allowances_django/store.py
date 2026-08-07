"""
The Django ORM implementation of :class:`allowances.store.Store`.

All of this library's coupling to Django lives here and in ``models.py``. Everything above it —
the ordering walk, the overdraft passes, the rounding, the window arithmetic — runs unchanged
against any other backend.
"""

from __future__ import annotations

import uuid

from datetime import datetime
from decimal import Decimal
from typing import Sequence

from django.db import transaction as db_transaction
from django.db.models import Q, Sum

from allowances.types import (
    FulfillmentRecord as CoreRecord,
    ObjectRef,
    SubjectRef,
    Transaction as CoreTransaction,
    TransactionKind,
    Wallet as CoreWallet,
    WalletType,
)
from allowances_django.models import (
    FulfillmentRecord,
    LicenseModifier,
    Transaction,
    TransactionArchive,
    Wallet,
    WalletArchive,
)


def _validity_q(at: datetime) -> Q:
    """Valid means: started (or never had a start) and not yet finished (or never ends)."""
    return (Q(valid_from__isnull=True) | Q(valid_from__lte=at)) & (Q(valid_until__isnull=True) | Q(valid_until__gte=at))


def _subject_q(subjects: Sequence[SubjectRef]) -> Q:
    query = Q()
    for subject in subjects:
        query |= Q(subject_type=subject.type, subject_id=subject.id)
    return query


def to_wallet(row: Wallet) -> CoreWallet:
    """Row to value object."""
    return CoreWallet(
        id=row.pk,
        subject=SubjectRef(row.subject_type, row.subject_id),
        wallet_type=WalletType(row.wallet_type),
        currency=row.currency,
        balance=row.balance,
        created_at=row.created_at,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        overdraft_limit=row.overdraft_limit,
        first_overdraft_at=row.first_overdraft_at,
        reset_config=row.reset_config,
    )


def to_transaction(row: Transaction, record_guid: uuid.UUID | None = None) -> CoreTransaction:
    """
    Row to value object. Pass ``record_guid`` when the caller already knows it, to save this from
    following the record foreign key.
    """
    if record_guid is None and row.record_id is not None:
        record_guid = row.record.guid
    return CoreTransaction(
        id=row.pk,
        wallet_id=row.wallet_id,
        kind=TransactionKind(row.kind),
        amount=row.amount,
        created_at=row.created_at,
        record_guid=record_guid,
        feature=row.feature,
        storno_of_id=row.storno_of_id,
        obj=ObjectRef(row.object_type, row.object_id) if row.object_type else None,
    )


def to_record(row: FulfillmentRecord) -> CoreRecord:
    return CoreRecord(guid=row.guid, created_at=row.created_at)


class DjangoStore:
    """
    A Store over the models in ``allowances_django.models``.

    Cheap to construct — it holds no connection of its own, only a small cache mapping fulfillment
    record guids to primary keys so writing a batch of transactions doesn't re-resolve the same
    record for each one.
    """

    def __init__(self, using: str | None = None):
        #: Database alias, for projects that route this app to its own connection.
        self.using = using
        self._record_ids: dict[uuid.UUID, int] = {}

    # -- helpers -------------------------------------------------------------------------------

    def _qs(self, model):
        return model.objects.using(self.using) if self.using else model.objects.all()

    def _record_id(self, guid: uuid.UUID | None) -> int | None:
        if guid is None:
            return None
        if guid not in self._record_ids:
            row = self._qs(FulfillmentRecord).filter(guid=guid).first()
            if row is None:
                return None
            self._record_ids[guid] = row.pk
        return self._record_ids[guid]

    # -- transactions / locking ----------------------------------------------------------------

    def unit_of_work(self):
        return db_transaction.atomic(using=self.using)

    # -- wallets -------------------------------------------------------------------------------

    def find_wallets(
        self,
        *,
        subjects: Sequence[SubjectRef],
        wallet_types: Sequence[WalletType],
        currencies: Sequence[str],
        at: datetime,
        lock: bool = False,
    ) -> list[CoreWallet]:
        if not subjects or not wallet_types or not currencies:
            return []
        qs = (
            self._qs(Wallet)
            .filter(_subject_q(subjects))
            .filter(
                wallet_type__in=[str(wallet_type) for wallet_type in wallet_types],
                currency__in=list(currencies),
            )
            .filter(_validity_q(at))
            .order_by("created_at", "id")
        )
        if lock:
            qs = qs.select_for_update()
        return [to_wallet(row) for row in qs]

    def get_wallet(self, wallet_id: object, *, lock: bool = False) -> CoreWallet | None:
        qs = self._qs(Wallet).filter(pk=wallet_id)
        if lock:
            qs = qs.select_for_update()
        row = qs.first()
        return to_wallet(row) if row else None

    def save_wallet(self, wallet: CoreWallet) -> None:
        self._qs(Wallet).filter(pk=wallet.id).update(
            balance=wallet.balance,
            first_overdraft_at=wallet.first_overdraft_at,
            valid_from=wallet.valid_from,
            valid_until=wallet.valid_until,
        )

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
    ) -> CoreWallet:
        row = Wallet(
            subject_type=subject.type,
            subject_id=subject.id,
            wallet_type=str(wallet_type),
            currency=currency,
            balance=Decimal(str(balance)),
            overdraft_limit=None if overdraft_limit is None else Decimal(str(overdraft_limit)),
            reset_config=reset_config,
            valid_from=valid_from,
            valid_until=valid_until,
        )
        row.save(using=self.using)
        return to_wallet(row)

    def close_wallets(self, *, subject: SubjectRef, at: datetime) -> int:
        return (
            self._qs(Wallet)
            .filter(subject_type=subject.type, subject_id=subject.id)
            .filter(_validity_q(at))
            .update(valid_until=at)
        )

    def find_expired_wallets(self, *, at: datetime, lock: bool = False) -> list[CoreWallet]:
        qs = self._qs(Wallet).filter(valid_until__isnull=False, valid_until__lte=at).order_by("created_at", "id")
        if lock:
            qs = qs.select_for_update()
        return [to_wallet(row) for row in qs]

    def archive_wallet(self, wallet: CoreWallet) -> None:
        row = self._qs(Wallet).filter(pk=wallet.id).first()
        if row is None:
            return

        archive = WalletArchive(
            original_wallet_id=row.pk,
            subject_type=row.subject_type,
            subject_id=row.subject_id,
            wallet_type=row.wallet_type,
            currency=row.currency,
            final_balance=row.balance,
            valid_from=row.valid_from,
            valid_until=row.valid_until,
            overdraft_limit=row.overdraft_limit,
            first_overdraft_at=row.first_overdraft_at,
            reset_config=row.reset_config,
            original_created_at=row.created_at,
        )
        archive.save(using=self.using)

        transactions = list(row.transactions.order_by("created_at", "id"))
        archived_by_id = {}
        for txn in transactions:
            copy = TransactionArchive(
                original_transaction_id=txn.pk,
                wallet_archive=archive,
                original_wallet_id=row.pk,
                record_id=txn.record_id,
                kind=txn.kind,
                amount=txn.amount,
                feature=txn.feature,
                object_type=txn.object_type,
                object_id=txn.object_id,
                original_created_at=txn.created_at,
            )
            copy.save(using=self.using)
            archived_by_id[txn.pk] = copy

        # Second pass: the storno links can only be rebuilt once every archive row exists.
        for txn in transactions:
            if txn.storno_of_id in archived_by_id:
                copy = archived_by_id[txn.pk]
                copy.storno_of = archived_by_id[txn.storno_of_id]
                copy.save(using=self.using, update_fields=["storno_of"])

        # Transaction.storno_of is PROTECT, and Django enforces that even when both rows are part of
        # the same cascade — so the reversing rows have to go first, leaving nothing pointing at
        # anything by the time the wallet's own delete cascades over the rest.
        storno_ids = [txn.pk for txn in transactions if txn.storno_of_id]
        if storno_ids:
            self._qs(Transaction).filter(pk__in=storno_ids).delete()
        row.delete()

    # -- ledger --------------------------------------------------------------------------------

    def sum_transactions(self, *, wallet_id: object, since: datetime, until: datetime) -> Decimal:
        total = (
            self._qs(Transaction)
            .filter(wallet_id=wallet_id, created_at__gte=since, created_at__lte=until)
            .aggregate(total=Sum("amount"))["total"]
        )
        return total or Decimal("0")

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
    ) -> CoreTransaction:
        row = Transaction(
            wallet_id=wallet_id,
            record_id=self._record_id(record_guid),
            kind=str(kind),
            amount=Decimal(str(amount)),
            feature=feature,
            storno_of_id=storno_of_id,
        )
        if at is not None:
            row.created_at = at
        row.save(using=self.using)
        return to_transaction(row, record_guid=record_guid)

    def transactions_for_record(self, *, record_guid: uuid.UUID, lock: bool = False) -> list[CoreTransaction]:
        record_id = self._record_id(record_guid)
        if record_id is None:
            return []
        # Filtering on record_id rather than record__guid keeps this join-free, which matters
        # because Postgres refuses to lock rows through a join.
        qs = self._qs(Transaction).filter(record_id=record_id).order_by("created_at", "id")
        if lock:
            qs = qs.select_for_update()
        return [to_transaction(row, record_guid=record_guid) for row in qs]

    def attach_object(self, *, transaction_ids: Sequence[object], obj: ObjectRef) -> None:
        self._qs(Transaction).filter(pk__in=list(transaction_ids)).update(object_type=obj.type, object_id=obj.id)

    # -- fulfillment records -------------------------------------------------------------------

    def get_or_create_record(self, guid: uuid.UUID | None = None) -> CoreRecord:
        if guid is None:
            row = FulfillmentRecord()
            row.save(using=self.using)
        else:
            row, _ = FulfillmentRecord.objects.using(self.using).get_or_create(guid=guid)
        self._record_ids[row.guid] = row.pk
        return to_record(row)

    def get_record(self, guid: uuid.UUID, *, lock: bool = False) -> CoreRecord | None:
        qs = self._qs(FulfillmentRecord).filter(guid=guid)
        if lock:
            qs = qs.select_for_update()
        row = qs.first()
        if row is None:
            return None
        self._record_ids[row.guid] = row.pk
        return to_record(row)

    def records_with_unattached_transactions(self, *, older_than: datetime | None = None) -> list[CoreRecord]:
        qs = self._qs(FulfillmentRecord).filter(transactions__object_type="").distinct()
        if older_than is not None:
            qs = qs.filter(created_at__lte=older_than)
        return [to_record(row) for row in qs.order_by("created_at", "id")]

    # -- modifiers -----------------------------------------------------------------------------

    def find_modifier_factors(self, *, feature: str, subjects: Sequence[SubjectRef], at: datetime) -> list[Decimal]:
        if not subjects:
            return []
        return list(
            self._qs(LicenseModifier)
            .filter(feature=feature)
            .filter(_subject_q(subjects))
            .filter(_validity_q(at))
            .values_list("factor", flat=True)
        )
