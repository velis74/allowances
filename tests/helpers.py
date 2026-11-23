"""
Test helpers for operations the Store port does not expose (seeding cost modifiers, counting and
reading rows), implemented for ``MemoryStore`` and for the stores that keep their data in the tables of
``allowances_django`` (the Django and adorm stores).

The database helpers read the committed rows with Django's asynchronous ORM.
"""

from decimal import Decimal

from allowances.backends import MemoryStore
from allowances.types import ObjectRef, SubjectRef, Transaction as CoreTransaction, TransactionKind, Wallet, WalletType


def _is_memory(store) -> bool:
    return isinstance(store, MemoryStore)


def _wallet(row) -> Wallet:
    return Wallet(
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


def _transaction(row) -> CoreTransaction:
    return CoreTransaction(
        id=row.pk,
        wallet_id=row.wallet_id,
        kind=TransactionKind(row.kind),
        amount=row.amount,
        created_at=row.created_at,
        record_guid=row.record.guid if row.record_id is not None else None,
        feature=row.feature,
        storno_of_id=row.storno_of_id,
        obj=ObjectRef(row.object_type, row.object_id) if row.object_type else None,
        actor=SubjectRef(row.actor_type, row.actor_id) if row.actor_type else None,
    )


async def add_modifier(store, *, subject, feature, factor, valid_from=None, valid_until=None):
    """Create a cost modifier in the store."""
    if _is_memory(store):
        return await store.add_modifier(
            subject=subject,
            feature=feature,
            factor=Decimal(str(factor)),
            valid_from=valid_from,
            valid_until=valid_until,
        )
    from allowances_django.models import LicenseModifier

    return await LicenseModifier.objects.acreate(
        subject_type=subject.type,
        subject_id=subject.id,
        feature=feature,
        factor=Decimal(str(factor)),
        valid_from=valid_from,
        valid_until=valid_until,
    )


async def transaction_count(store) -> int:
    if _is_memory(store):
        return len(store._transactions)
    from allowances_django.models import Transaction

    return await Transaction.objects.acount()


async def transactions_for_wallet(store, wallet_id) -> list:
    if _is_memory(store):
        return [t for t in store._transactions.values() if t.wallet_id == wallet_id]
    from allowances_django.models import Transaction

    rows = Transaction.objects.filter(wallet_id=wallet_id).select_related("record").order_by("created_at", "id")
    return [_transaction(row) async for row in rows]


async def archived_wallet_ids(store) -> list:
    if _is_memory(store):
        return [w.id for w in store.archived_wallets]
    from allowances_django.models import WalletArchive

    return [pk async for pk in WalletArchive.objects.values_list("original_wallet_id", flat=True)]


async def archived_wallet_balance(store, wallet_id):
    if _is_memory(store):
        return next(w.balance for w in store.archived_wallets if w.id == wallet_id)
    from allowances_django.models import WalletArchive

    return (await WalletArchive.objects.aget(original_wallet_id=wallet_id)).final_balance


async def archived_transaction_amounts(store, wallet_id) -> list:
    if _is_memory(store):
        return [t.amount for t in store.archived_transactions if t.wallet_id == wallet_id]
    from allowances_django.models import TransactionArchive

    rows = TransactionArchive.objects.filter(original_wallet_id=wallet_id).values_list("amount", flat=True)
    return [amount async for amount in rows]


async def wallets_for_subject(store, subject) -> list:
    """Return every wallet a subject holds, by id, valid or not (``find_wallets`` returns only valid ones)."""
    if _is_memory(store):
        return sorted((w for w in store._wallets.values() if w.subject == subject), key=lambda w: w.id)
    from allowances_django.models import Wallet as WalletRow

    rows = WalletRow.objects.filter(subject_type=subject.type, subject_id=subject.id).order_by("id")
    return [_wallet(row) async for row in rows]
