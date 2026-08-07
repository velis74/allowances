"""
The few things tests need that the Store port deliberately doesn't expose.

Seeding modifiers and counting rows are not the engine's business, so they are not on the port —
but assertions need them, and they have to work against either backend.
"""

from decimal import Decimal

from allowances.backends import MemoryStore


def _is_memory(store) -> bool:
    return isinstance(store, MemoryStore)


def add_modifier(store, *, subject, feature, factor, valid_from=None, valid_until=None):
    """Seed a cost modifier. Applications create these themselves; the engine only reads them."""
    if _is_memory(store):
        return store.add_modifier(
            subject=subject,
            feature=feature,
            factor=Decimal(str(factor)),
            valid_from=valid_from,
            valid_until=valid_until,
        )
    from allowances_django.models import LicenseModifier

    return LicenseModifier.objects.create(
        subject_type=subject.type,
        subject_id=subject.id,
        feature=feature,
        factor=Decimal(str(factor)),
        valid_from=valid_from,
        valid_until=valid_until,
    )


def transaction_count(store) -> int:
    if _is_memory(store):
        return len(store._transactions)
    from allowances_django.models import Transaction

    return Transaction.objects.count()


def transactions_for_wallet(store, wallet_id) -> list:
    if _is_memory(store):
        return [t for t in store._transactions.values() if t.wallet_id == wallet_id]
    from allowances_django.models import Transaction
    from allowances_django.store import to_transaction

    return [to_transaction(row) for row in Transaction.objects.filter(wallet_id=wallet_id).order_by("created_at", "id")]


def archived_wallet_ids(store) -> list:
    if _is_memory(store):
        return [w.id for w in store.archived_wallets]
    from allowances_django.models import WalletArchive

    return list(WalletArchive.objects.values_list("original_wallet_id", flat=True))


def archived_wallet_balance(store, wallet_id):
    if _is_memory(store):
        return next(w.balance for w in store.archived_wallets if w.id == wallet_id)
    from allowances_django.models import WalletArchive

    return WalletArchive.objects.get(original_wallet_id=wallet_id).final_balance


def archived_transaction_amounts(store, wallet_id) -> list:
    if _is_memory(store):
        return [t.amount for t in store.archived_transactions if t.wallet_id == wallet_id]
    from allowances_django.models import TransactionArchive

    return list(TransactionArchive.objects.filter(original_wallet_id=wallet_id).values_list("amount", flat=True))


def wallets_for_subject(store, subject) -> list:
    """Every wallet a subject holds, valid or not — ``find_wallets`` only returns valid ones."""
    if _is_memory(store):
        return sorted((w for w in store._wallets.values() if w.subject == subject), key=lambda w: w.id)
    from allowances_django.models import Wallet
    from allowances_django.store import to_wallet

    rows = Wallet.objects.filter(subject_type=subject.type, subject_id=subject.id).order_by("id")
    return [to_wallet(row) for row in rows]
