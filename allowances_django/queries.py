"""
The Django ORM queries of both Django stores.

:class:`DjangoQueries` implements every operation of :class:`allowances.store.Store` as a synchronous
method on the Django ORM. :class:`allowances_django.store.DjangoStore` runs these methods on threads for the
asynchronous API; :class:`allowances_django.sync.store.DjangoStore` runs them directly on the calling thread.
"""

from __future__ import annotations

import uuid

from datetime import datetime
from decimal import Decimal
from typing import Sequence

from django.core.exceptions import ValidationError
from django.db import connections, IntegrityError, OperationalError, transaction as db_transaction
from django.db.models import Q, Sum

from allowances.types import (
    FulfillmentRecord as CoreRecord,
    ObjectRef,
    PLAN_SOURCE,
    SubjectRef,
    Transaction as CoreTransaction,
    TransactionKind,
    Wallet as CoreWallet,
    WalletType,
)
from allowances_django.models import (
    AllowancesLock,
    FulfillmentRecord,
    LicenseModifier,
    Membership,
    Transaction,
    TransactionArchive,
    Wallet,
    WalletArchive,
)
from allowances_django.subjects import resolve

#: Name of the :class:`~allowances_django.models.AllowancesLock` row that serialises membership changes.
MEMBERSHIP_LOCK_NAME = "membership"


def _validity_q(at: datetime) -> Q:
    """Rows valid at ``at``: ``valid_from`` empty or not after it, ``valid_until`` empty or after it."""
    return (Q(valid_from__isnull=True) | Q(valid_from__lte=at)) & (Q(valid_until__isnull=True) | Q(valid_until__gt=at))


def _subject_q(subjects: Sequence[SubjectRef]) -> Q:
    query = Q()
    for subject in subjects:
        query |= Q(subject_type=subject.type, subject_id=subject.id)
    return query


def _wallet_pks(wallet_ids) -> list:
    """The wallet ids as primary-key values (``'5'`` and ``5`` are one id); ids that cannot be one are dropped."""
    field = Wallet._meta.pk
    pks = set()
    for wallet_id in wallet_ids:
        try:
            pks.add(field.to_python(wallet_id))
        except ValidationError:
            continue
    return list(pks)


def _locked(qs):
    """
    Lock the selected rows with ``SELECT ... FOR UPDATE`` where the backend supports it; otherwise
    return the queryset unchanged.

    SQLite relies on its database-wide write lock, taken at ``BEGIN`` with ``transaction_mode:
    "IMMEDIATE"`` (check ``allowances.W001``). Without that mode a concurrent write fails with
    ``database is locked`` instead of waiting.
    """
    return qs.select_for_update() if connections[qs.db].features.has_select_for_update else qs


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
        source=row.source,
        allotment=row.allotment,
        scheduled=row.scheduled,
    )


def to_transaction(row: Transaction, record_guid: uuid.UUID | None = None) -> CoreTransaction:
    """Row to value object. A given ``record_guid`` is used instead of following the record foreign key."""
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
        actor=SubjectRef(row.actor_type, row.actor_id) if row.actor_type else None,
        metadata=row.metadata,
    )


def to_record(row: FulfillmentRecord) -> CoreRecord:
    return CoreRecord(guid=row.guid, created_at=row.created_at)


class DjangoQueries:
    """
    The store operations as synchronous methods over the models in ``allowances_django.models``, plus
    :meth:`resolve` and :meth:`live_wallet_ids`, which the Django shortcuts use.

    Holds no connection; caches fulfilment record guid → primary key. Every method uses the connection
    of the calling thread.
    """

    def __init__(self, using: str | None = None):
        #: Database alias of the allowances tables; ``None`` for the default database.
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

    def atomic(self):
        """A ``transaction.atomic`` block on the store's database."""
        return db_transaction.atomic(using=self.using)

    def lock_wallets(self, wallet_ids: Sequence[object]) -> list[CoreWallet]:
        ids = _wallet_pks(wallet_ids)
        if not ids:
            return []
        # ORDER BY under FOR UPDATE makes PostgreSQL take the row locks in ascending id order.
        qs = _locked(self._qs(Wallet).filter(pk__in=ids).order_by("id"))
        return [to_wallet(row) for row in qs]

    # -- wallets -------------------------------------------------------------------------------

    def find_wallets(
        self,
        *,
        subjects: Sequence[SubjectRef],
        wallet_types: Sequence[WalletType],
        currencies: Sequence[str] | None,
        at: datetime,
        lock: bool = False,
    ) -> list[CoreWallet]:
        if not subjects or not wallet_types or (currencies is not None and not currencies):
            return []
        qs = (
            self._qs(Wallet)
            .filter(_subject_q(subjects))
            .filter(wallet_type__in=[str(wallet_type) for wallet_type in wallet_types])
            .filter(_validity_q(at))
            .order_by("created_at", "id")
        )
        if currencies is not None:
            qs = qs.filter(currency__in=list(currencies))
        if lock:
            qs = _locked(qs)
        return [to_wallet(row) for row in qs]

    def get_wallet(self, wallet_id: object, *, lock: bool = False) -> CoreWallet | None:
        qs = self._qs(Wallet).filter(pk=wallet_id)
        if lock:
            qs = _locked(qs)
        row = qs.first()
        return to_wallet(row) if row else None

    def save_wallet(self, wallet: CoreWallet) -> None:
        self._qs(Wallet).filter(pk=wallet.id).update(
            balance=wallet.balance,
            first_overdraft_at=wallet.first_overdraft_at,
            valid_from=wallet.valid_from,
            valid_until=wallet.valid_until,
            overdraft_limit=wallet.overdraft_limit,
            reset_config=wallet.reset_config,
            allotment=wallet.allotment,
            scheduled=wallet.scheduled,
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
        source: str = PLAN_SOURCE,
        allotment: Decimal | None = None,
        scheduled: dict | None = None,
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
            source=source,
            allotment=None if allotment is None else Decimal(str(allotment)),
            scheduled=scheduled,
        )
        row.save(using=self.using)
        return to_wallet(row)

    def find_scheduled_wallets(self, *, subject: SubjectRef, lock: bool = False) -> list[CoreWallet]:
        qs = (
            self._qs(Wallet)
            .filter(subject_type=subject.type, subject_id=subject.id, scheduled__isnull=False)
            .order_by("created_at", "id")
        )
        if lock:
            qs = _locked(qs)
        return [to_wallet(row) for row in qs]

    def find_expired_wallets(self, *, at: datetime, lock: bool = False) -> list[CoreWallet]:
        qs = self._qs(Wallet).filter(valid_until__isnull=False, valid_until__lte=at).order_by("created_at", "id")
        if lock:
            qs = _locked(qs)
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
            source=row.source,
            allotment=row.allotment,
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
                actor_type=txn.actor_type,
                actor_id=txn.actor_id,
                metadata=txn.metadata,
                original_created_at=txn.created_at,
            )
            copy.save(using=self.using)
            archived_by_id[txn.pk] = copy

        # Storno links between archive rows, set once every archive row exists.
        for txn in transactions:
            if txn.storno_of_id in archived_by_id:
                copy = archived_by_id[txn.pk]
                copy.storno_of = archived_by_id[txn.storno_of_id]
                copy.save(using=self.using, update_fields=["storno_of"])

        # Transaction.storno_of is PROTECT, also within one cascade: delete storno rows before the wallet.
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

    def transactions_for_wallet(self, *, wallet_id: object, since: datetime, until: datetime) -> list[CoreTransaction]:
        qs = (
            self._qs(Transaction)
            .filter(wallet_id=wallet_id, created_at__gte=since, created_at__lte=until)
            .select_related("record")
            .order_by("created_at", "id")
        )
        return [to_transaction(row) for row in qs]

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
        metadata: dict | None = None,
    ) -> CoreTransaction:
        row = Transaction(
            wallet_id=wallet_id,
            record_id=self._record_id(record_guid),
            kind=str(kind),
            amount=Decimal(str(amount)),
            feature=feature,
            storno_of_id=storno_of_id,
            actor_type=actor.type if actor is not None else "",
            actor_id=actor.id if actor is not None else "",
            metadata=metadata,
        )
        if at is not None:
            row.created_at = at
        row.save(using=self.using)
        return to_transaction(row, record_guid=record_guid)

    def transactions_for_record(self, *, record_guid: uuid.UUID, lock: bool = False) -> list[CoreTransaction]:
        record_id = self._record_id(record_guid)
        if record_id is None:
            return []
        # Filter on record_id, not record__guid: no join, so FOR UPDATE locks only transaction rows.
        qs = self._qs(Transaction).filter(record_id=record_id).order_by("created_at", "id")
        if lock:
            qs = _locked(qs)
        return [to_transaction(row, record_guid=record_guid) for row in qs]

    def transactions_for_object(self, *, obj: ObjectRef) -> list[CoreTransaction]:
        qs = (
            self._qs(Transaction)
            .filter(object_type=obj.type, object_id=obj.id)
            .select_related("record")
            .order_by("created_at", "id")
        )
        return [to_transaction(row) for row in qs]

    def attach_object(self, *, transaction_ids: Sequence[object], obj: ObjectRef) -> None:
        self._qs(Transaction).filter(pk__in=list(transaction_ids)).update(object_type=obj.type, object_id=obj.id)

    # -- fulfilment records -------------------------------------------------------------------

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
        if lock and connections[qs.db].features.has_select_for_no_key_update:
            # FOR NO KEY UPDATE: archiving a wallet inserts rows referencing the record, and their
            # foreign-key check (FOR KEY SHARE) must not wait for a lock held across lock_wallets.
            qs = qs.select_for_update(no_key=True)
        elif lock:
            qs = _locked(qs)
        row = qs.first()
        if row is None:
            return None
        self._record_ids[row.guid] = row.pk
        return to_record(row)

    def records_with_unattached_transactions(self, *, older_than: datetime | None = None) -> list[CoreRecord]:
        qs = (
            self._qs(FulfillmentRecord)
            .filter(transactions__kind=str(TransactionKind.CONSUMPTION), transactions__object_type="")
            .distinct()
        )
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

    # -- memberships ---------------------------------------------------------------------------

    def add_membership(self, *, member: SubjectRef, group: SubjectRef) -> None:
        self._qs(Membership).get_or_create(
            member_type=member.type, member_id=member.id, group_type=group.type, group_id=group.id
        )

    def remove_membership(self, *, member: SubjectRef, group: SubjectRef) -> bool:
        deleted, _ = (
            self._qs(Membership)
            .filter(member_type=member.type, member_id=member.id, group_type=group.type, group_id=group.id)
            .delete()
        )
        return deleted > 0

    def memberships_of(self, *, members: Sequence[SubjectRef]) -> list[tuple[SubjectRef, SubjectRef]]:
        if not members:
            return []
        query = Q()
        for member in members:
            query |= Q(member_type=member.type, member_id=member.id)
        rows = self._qs(Membership).filter(query).order_by("created_at", "id")
        return [(SubjectRef(row.member_type, row.member_id), SubjectRef(row.group_type, row.group_id)) for row in rows]

    def lock_memberships(self) -> None:
        """
        Lock the ``AllowancesLock`` row ``"membership"`` with ``SELECT ... FOR UPDATE`` until the unit of
        work ends. On SQLite, IMMEDIATE transactions serialise writers and no row is locked.

        Migration 0004 creates the row; a missing row (for example after the table was flushed) is created
        in the unit of work. When a concurrent unit of work created it and the isolation level hides it
        (REPEATABLE READ, SERIALIZABLE), :class:`~django.db.OperationalError` is raised; a retry succeeds.
        """
        rows = self._qs(AllowancesLock).filter(name=MEMBERSHIP_LOCK_NAME)
        if not connections[rows.db].features.has_select_for_update:
            return
        if list(rows.select_for_update().values_list("pk", flat=True)):
            return
        try:
            with db_transaction.atomic(using=rows.db):
                self._qs(AllowancesLock).create(name=MEMBERSHIP_LOCK_NAME)
        except IntegrityError:
            # Created by a concurrent unit of work: wait for its lock.
            if not list(rows.select_for_update().values_list("pk", flat=True)):
                raise OperationalError(
                    f"AllowancesLock row {MEMBERSHIP_LOCK_NAME!r} exists but is not visible in this transaction; retry."
                ) from None

    # -- Django shortcuts ----------------------------------------------------------------------

    def resolve(self, ref: SubjectRef | ObjectRef):
        """The model instance behind ``ref``, or ``None``; see :func:`allowances_django.subjects.resolve`."""
        return resolve(ref)

    def live_wallet_ids(
        self, *, subject: SubjectRef, wallet_type: WalletType, currency: str, at: datetime
    ) -> list[object]:
        """
        Ids of the live wallets of ``subject`` with this type and currency whose ``valid_until`` is empty or
        after ``at``, wallets not yet valid at ``at`` included. Ascending.
        """
        return sorted(
            self._qs(Wallet)
            .filter(subject_type=subject.type, subject_id=subject.id, wallet_type=str(wallet_type), currency=currency)
            .exclude(valid_until__lte=at)
            .values_list("pk", flat=True)
        )
