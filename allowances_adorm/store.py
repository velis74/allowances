"""
The adorm implementation of :class:`allowances.store.Store`.

Every method runs its queries through adorm on the event loop: on adorm's Django driver (the Django ORM on a thread
of a pool slot) or on its native PostgreSQL driver (asyncpg, no threads). The models are
:mod:`allowances_adorm.models`, generated from ``allowances_django.models``; the tables and migrations are
``allowances_django``'s.

The methods suspend, so the store serves only the asynchronous API (:mod:`allowances`), not :mod:`allowances.sync`.
"""

from __future__ import annotations

import uuid

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncIterator, Sequence

import adorm

from adorm import Q, Sum
from adorm.settings import profiles

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
from allowances_adorm.models.allowances import (
    AllowancesLock,
    FulfillmentRecord,
    LicenseModifier,
    Membership,
    Transaction,
    TransactionArchive,
    Wallet,
    WalletArchive,
)

#: Name of the ``AllowancesLock`` row that serialises membership changes.
MEMBERSHIP_LOCK_NAME = "membership"

_NO_KEY_LOCK = "clause.select_for_update.no_key"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validity_q(at: datetime) -> Q:
    """Rows valid at ``at``: ``valid_from`` empty or not after it, ``valid_until`` empty or after it."""
    return (Q(valid_from__isnull=True) | Q(valid_from__lte=at)) & (Q(valid_until__isnull=True) | Q(valid_until__gt=at))


def _subject_q(subjects: Sequence[SubjectRef]) -> Q:
    query = Q()
    for subject in subjects:
        query |= Q(subject_type=subject.type, subject_id=subject.id)
    return query


def _wallet_pks(wallet_ids) -> list[int]:
    """The wallet ids as primary-key values (``'5'`` and ``5`` are one id); ids that cannot be one are dropped."""
    pks = set()
    for wallet_id in wallet_ids:
        if isinstance(wallet_id, bool):
            continue
        try:
            pks.add(int(wallet_id))
        except (TypeError, ValueError):
            continue
    return list(pks)


def _decimal(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def to_wallet(row: Wallet) -> CoreWallet:
    """Row to value object."""
    return CoreWallet(
        id=row.id,
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


def to_transaction(row: Transaction, record_guid: uuid.UUID | None) -> CoreTransaction:
    """Row to value object; ``record_guid`` is the guid of the row's fulfilment record."""
    return CoreTransaction(
        id=row.id,
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


class AdormStore:
    """
    A Store over the adorm models in :mod:`allowances_adorm.models`.

    ``using`` names the adorm connection profile. Without it, each call runs on adorm's current session
    (``adorm.current_session()``), so the store's work joins an ``adorm.atomic()`` block the caller has open on that
    session. With it, the store runs on the session ``(using, session_name)``.

    Holds no connection; caches fulfilment record guid → primary key.
    """

    def __init__(self, using: str | None = None, *, session_name: str = "default"):
        #: The adorm profile; ``None`` for the current session.
        self.using = using
        self._session = None if using is None else adorm.create_session(using, session_name)
        self._record_ids: dict[uuid.UUID, int] = {}

    # -- helpers -------------------------------------------------------------------------------

    def session(self) -> adorm.Session:
        """The adorm session the next call runs on."""
        return self._session if self._session is not None else adorm.current_session()

    def _m(self, model):
        return self.session().bind(model)

    async def _has(self, construct: str) -> bool:
        """Whether the session's profile supports ``construct`` (an adorm construct name)."""
        profile = await profiles.resolve(self.session().using)
        return construct in profile.get_driver().capabilities_for(profile).constructs

    async def _record_id(self, guid: uuid.UUID | None) -> int | None:
        if guid is None:
            return None
        if guid not in self._record_ids:
            pk = await self._m(FulfillmentRecord).filter(guid=guid).values_list("id", flat=True).first()
            if pk is None:
                return None
            self._record_ids[guid] = pk
        return self._record_ids[guid]

    async def _record_guids(self, record_ids) -> dict[int, uuid.UUID]:
        ids = {record_id for record_id in record_ids if record_id is not None}
        if not ids:
            return {}
        rows = self._m(FulfillmentRecord).filter(id__in=list(ids)).values_list("id", "guid")
        return {pk: guid async for pk, guid in rows}

    async def _wallets(self, qs) -> list[CoreWallet]:
        return [to_wallet(row) async for row in qs]

    # -- transactions / locking ----------------------------------------------------------------

    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator[None]:
        """An ``adorm.atomic()`` block on the session; a nested block is a savepoint."""
        async with self.session().atomic():
            yield

    async def lock_wallets(self, wallet_ids: Sequence[object]) -> list[CoreWallet]:
        ids = _wallet_pks(wallet_ids)
        if not ids:
            return []
        # ORDER BY under FOR UPDATE makes PostgreSQL take the row locks in ascending id order.
        return await self._wallets(self._m(Wallet).filter(id__in=ids).order_by("id").select_for_update())

    # -- wallets -------------------------------------------------------------------------------

    async def find_wallets(
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
            self._m(Wallet)
            .filter(_subject_q(subjects))
            .filter(wallet_type__in=[str(wallet_type) for wallet_type in wallet_types])
            .filter(_validity_q(at))
            .order_by("created_at", "id")
        )
        if currencies is not None:
            qs = qs.filter(currency__in=list(currencies))
        if lock:
            qs = qs.select_for_update()
        return await self._wallets(qs)

    async def get_wallet(self, wallet_id: object, *, lock: bool = False) -> CoreWallet | None:
        ids = _wallet_pks([wallet_id])
        if not ids:
            return None
        qs = self._m(Wallet).filter(id=ids[0])
        if lock:
            qs = qs.select_for_update()
        row = await qs.first()
        return to_wallet(row) if row else None

    async def save_wallet(self, wallet: CoreWallet) -> None:
        await (
            self._m(Wallet)
            .filter(id=wallet.id)
            .update(
                balance=wallet.balance,
                first_overdraft_at=wallet.first_overdraft_at,
                valid_from=wallet.valid_from,
                valid_until=wallet.valid_until,
                overdraft_limit=wallet.overdraft_limit,
                reset_config=wallet.reset_config,
                allotment=wallet.allotment,
                scheduled=wallet.scheduled,
                updated_at=_now(),
            )
        )

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
    ) -> CoreWallet:
        now = _now()
        row = await self._m(Wallet).create(
            subject_type=subject.type,
            subject_id=subject.id,
            wallet_type=str(wallet_type),
            currency=currency,
            balance=_decimal(balance),
            overdraft_limit=_decimal(overdraft_limit),
            reset_config=reset_config,
            valid_from=valid_from,
            valid_until=valid_until,
            source=source,
            allotment=_decimal(allotment),
            scheduled=scheduled,
            created_at=now,
            updated_at=now,
        )
        return to_wallet(row)

    async def find_scheduled_wallets(self, *, subject: SubjectRef, lock: bool = False) -> list[CoreWallet]:
        qs = (
            self._m(Wallet)
            .filter(subject_type=subject.type, subject_id=subject.id, scheduled__isnull=False)
            .order_by("created_at", "id")
        )
        if lock:
            qs = qs.select_for_update()
        return await self._wallets(qs)

    async def find_expired_wallets(self, *, at: datetime, lock: bool = False) -> list[CoreWallet]:
        qs = self._m(Wallet).filter(valid_until__isnull=False, valid_until__lte=at).order_by("created_at", "id")
        if lock:
            qs = qs.select_for_update()
        return await self._wallets(qs)

    async def archive_wallet(self, wallet: CoreWallet) -> None:
        ids = _wallet_pks([wallet.id])
        row = await self._m(Wallet).filter(id=ids[0]).first() if ids else None
        if row is None:
            return

        archive = await self._m(WalletArchive).create(
            original_wallet_id=row.id,
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
            archived_at=_now(),
        )

        transactions = [txn async for txn in self._m(Transaction).filter(wallet_id=row.id).order_by("created_at", "id")]
        archived_at = _now()
        copies = await self._m(TransactionArchive).bulk_create(
            [
                TransactionArchive(
                    original_transaction_id=txn.id,
                    wallet_archive_id=archive.id,
                    original_wallet_id=row.id,
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
                    archived_at=archived_at,
                )
                for txn in transactions
            ]
        )
        archived_by_id = {txn.id: copy for txn, copy in zip(transactions, copies)}

        # Storno links between archive rows, set once every archive row exists.
        for txn in transactions:
            if txn.storno_of_id in archived_by_id:
                await (
                    self._m(TransactionArchive)
                    .filter(id=archived_by_id[txn.id].id)
                    .update(storno_of_id=archived_by_id[txn.storno_of_id].id)
                )

        # Transaction.storno_of is PROTECT, also within one cascade: delete storno rows before the wallet.
        storno_ids = [txn.id for txn in transactions if txn.storno_of_id]
        if storno_ids:
            await self._m(Transaction).filter(id__in=storno_ids).delete()
        await self._m(Wallet).filter(id=row.id).delete()

    # -- ledger --------------------------------------------------------------------------------

    async def sum_transactions(self, *, wallet_id: object, since: datetime, until: datetime) -> Decimal:
        result = await (
            self._m(Transaction)
            .filter(wallet_id=wallet_id, created_at__gte=since, created_at__lte=until)
            .aggregate(total=Sum("amount"))
        )
        return result["total"] or Decimal("0")

    async def transactions_for_wallet(
        self, *, wallet_id: object, since: datetime, until: datetime
    ) -> list[CoreTransaction]:
        rows = [
            row
            async for row in self._m(Transaction)
            .filter(wallet_id=wallet_id, created_at__gte=since, created_at__lte=until)
            .order_by("created_at", "id")
        ]
        guids = await self._record_guids(row.record_id for row in rows)
        return [to_transaction(row, guids.get(row.record_id)) for row in rows]

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
    ) -> CoreTransaction:
        record_id = await self._record_id(record_guid)
        row = await self._m(Transaction).create(
            wallet_id=wallet_id,
            record_id=record_id,
            kind=str(kind),
            amount=_decimal(amount),
            feature=feature,
            storno_of_id=storno_of_id,
            actor_type=actor.type if actor is not None else "",
            actor_id=actor.id if actor is not None else "",
            metadata=metadata,
            created_at=at if at is not None else _now(),
        )
        return to_transaction(row, record_guid)

    async def transactions_for_record(self, *, record_guid: uuid.UUID, lock: bool = False) -> list[CoreTransaction]:
        record_id = await self._record_id(record_guid)
        if record_id is None:
            return []
        # Filter on record_id, not record__guid: no join, so FOR UPDATE locks only transaction rows.
        qs = self._m(Transaction).filter(record_id=record_id).order_by("created_at", "id")
        if lock:
            qs = qs.select_for_update()
        return [to_transaction(row, record_guid) async for row in qs]

    async def transactions_for_object(self, *, obj: ObjectRef) -> list[CoreTransaction]:
        rows = [
            row
            async for row in self._m(Transaction)
            .filter(object_type=obj.type, object_id=obj.id)
            .order_by("created_at", "id")
        ]
        guids = await self._record_guids(row.record_id for row in rows)
        return [to_transaction(row, guids.get(row.record_id)) for row in rows]

    async def attach_object(self, *, transaction_ids: Sequence[object], obj: ObjectRef) -> None:
        ids = list(transaction_ids)
        if ids:
            await self._m(Transaction).filter(id__in=ids).update(object_type=obj.type, object_id=obj.id)

    # -- fulfilment records -------------------------------------------------------------------

    async def get_or_create_record(self, guid: uuid.UUID | None = None) -> CoreRecord:
        now = _now()
        if guid is None:
            row = await self._m(FulfillmentRecord).create(guid=uuid.uuid4(), created_at=now, updated_at=now)
        else:
            row, _ = await self._m(FulfillmentRecord).get_or_create(
                guid=guid, defaults={"created_at": now, "updated_at": now}
            )
        self._record_ids[row.guid] = row.id
        return to_record(row)

    async def get_record(self, guid: uuid.UUID, *, lock: bool = False) -> CoreRecord | None:
        qs = self._m(FulfillmentRecord).filter(guid=guid)
        if lock and await self._has(_NO_KEY_LOCK):
            # FOR NO KEY UPDATE: archiving a wallet inserts rows referencing the record, and their
            # foreign-key check (FOR KEY SHARE) must not wait for a lock held across lock_wallets.
            qs = qs.select_for_update(no_key=True)
        elif lock:
            qs = qs.select_for_update()
        row = await qs.first()
        if row is None:
            return None
        self._record_ids[row.guid] = row.id
        return to_record(row)

    async def records_with_unattached_transactions(self, *, older_than: datetime | None = None) -> list[CoreRecord]:
        qs = (
            self._m(FulfillmentRecord)
            .filter(transactions__kind=str(TransactionKind.CONSUMPTION), transactions__object_type="")
            .distinct()
        )
        if older_than is not None:
            qs = qs.filter(created_at__lte=older_than)
        return [to_record(row) async for row in qs.order_by("created_at", "id")]

    # -- modifiers -----------------------------------------------------------------------------

    async def find_modifier_factors(
        self, *, feature: str, subjects: Sequence[SubjectRef], at: datetime
    ) -> list[Decimal]:
        if not subjects:
            return []
        qs = (
            self._m(LicenseModifier)
            .filter(feature=feature)
            .filter(_subject_q(subjects))
            .filter(_validity_q(at))
            .values_list("factor", flat=True)
        )
        return [factor async for factor in qs]

    # -- memberships ---------------------------------------------------------------------------

    async def add_membership(self, *, member: SubjectRef, group: SubjectRef) -> None:
        await self._m(Membership).get_or_create(
            member_type=member.type,
            member_id=member.id,
            group_type=group.type,
            group_id=group.id,
            defaults={"created_at": _now()},
        )

    async def remove_membership(self, *, member: SubjectRef, group: SubjectRef) -> bool:
        deleted, _ = await (
            self._m(Membership)
            .filter(member_type=member.type, member_id=member.id, group_type=group.type, group_id=group.id)
            .delete()
        )
        return deleted > 0

    async def memberships_of(self, *, members: Sequence[SubjectRef]) -> list[tuple[SubjectRef, SubjectRef]]:
        if not members:
            return []
        query = Q()
        for member in members:
            query |= Q(member_type=member.type, member_id=member.id)
        rows = self._m(Membership).filter(query).order_by("created_at", "id")
        return [
            (SubjectRef(row.member_type, row.member_id), SubjectRef(row.group_type, row.group_id)) async for row in rows
        ]

    # -- Django shortcuts ----------------------------------------------------------------------

    async def resolve(self, ref: SubjectRef | ObjectRef):
        """The Django model instance behind ``ref``, or ``None``; see :func:`allowances_django.subjects.resolve`."""
        from asgiref.sync import sync_to_async

        from allowances_django.subjects import resolve

        return await sync_to_async(resolve)(ref)

    async def live_wallet_ids(
        self, *, subject: SubjectRef, wallet_type: WalletType, currency: str, at: datetime
    ) -> list[object]:
        """
        Ids of the live wallets of ``subject`` with this type and currency whose ``valid_until`` is empty or
        after ``at``, wallets not yet valid at ``at`` included. Ascending.
        """
        rows = (
            self._m(Wallet)
            .filter(subject_type=subject.type, subject_id=subject.id, wallet_type=str(wallet_type), currency=currency)
            .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=at))
            .order_by("id")
            .values_list("id", flat=True)
        )
        return [pk async for pk in rows]

    async def _membership_lock_rows(self) -> list[str]:
        rows = self._m(AllowancesLock).filter(name=MEMBERSHIP_LOCK_NAME).select_for_update()
        return [name async for name in rows.values_list("name", flat=True)]

    async def lock_memberships(self) -> None:
        """
        Lock the ``AllowancesLock`` row ``"membership"`` with ``SELECT ... FOR UPDATE`` until the unit of work
        ends. On SQLite the transaction holds the database's write lock from ``BEGIN IMMEDIATE`` on.

        A missing row is created in a savepoint of the unit of work. When a concurrent unit of work created it and
        the isolation level hides it (REPEATABLE READ, SERIALIZABLE), :class:`adorm.OperationalError` is raised; a
        retry succeeds.
        """
        if await self._membership_lock_rows():
            return
        try:
            async with self.session().atomic():
                await self._m(AllowancesLock).create(name=MEMBERSHIP_LOCK_NAME)
        except adorm.IntegrityError:
            # Created by a concurrent unit of work: wait for its lock.
            if not await self._membership_lock_rows():
                raise adorm.OperationalError(
                    f"AllowancesLock row {MEMBERSHIP_LOCK_NAME!r} exists but is not visible in this transaction; retry."
                ) from None
