from decimal import Decimal

import pytest

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction as db_transaction

from django_licensing.models import FulfillmentRecord, Transaction, TransactionKind, Wallet, WalletType

pytestmark = pytest.mark.django_db


@pytest.fixture
def subject():
    return User.objects.create(username="bob")


@pytest.fixture
def wallet(subject):
    return Wallet.objects.create(
        subject=subject, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("100")
    )


def test_create_grant_transaction(wallet):
    record = FulfillmentRecord.objects.create()

    txn = Transaction.objects.create(
        wallet=wallet,
        fulfillment_record=record,
        kind=TransactionKind.GRANT,
        amount=Decimal("10"),
        feature="send_email",
    )
    txn.refresh_from_db()

    assert txn.kind == TransactionKind.GRANT
    assert txn.amount == Decimal("10.000000")
    assert txn.wallet_id == wallet.id
    assert txn.fulfillment_record_id == record.id
    assert txn.feature == "send_email"
    assert txn.storno_of is None
    assert txn.attached_object is None
    assert txn.created_at is not None


def test_consumption_transaction_has_negative_amount(wallet):
    txn = Transaction.objects.create(wallet=wallet, kind=TransactionKind.CONSUMPTION, amount=Decimal("-0.5"))
    txn.refresh_from_db()

    assert txn.amount == Decimal("-0.500000")


def test_fulfillment_record_is_optional(wallet):
    # Ad-hoc/manual ledger entries should not require a synthetic FulfillmentRecord.
    txn = Transaction.objects.create(wallet=wallet, kind=TransactionKind.GRANT, amount=Decimal("1"))

    assert txn.fulfillment_record is None


def test_storno_of_self_fk_links_to_original_transaction(wallet):
    original = Transaction.objects.create(wallet=wallet, kind=TransactionKind.CONSUMPTION, amount=Decimal("-5"))

    storno = Transaction.objects.create(
        wallet=wallet, kind=TransactionKind.STORNO, amount=Decimal("5"), storno_of=original
    )
    storno.refresh_from_db()

    assert storno.storno_of_id == original.id
    assert original.storno_transactions.get() == storno


def test_storno_of_prevents_double_storno(wallet):
    original = Transaction.objects.create(wallet=wallet, kind=TransactionKind.CONSUMPTION, amount=Decimal("-5"))
    Transaction.objects.create(wallet=wallet, kind=TransactionKind.STORNO, amount=Decimal("5"), storno_of=original)

    with pytest.raises(IntegrityError):
        with db_transaction.atomic():
            Transaction.objects.create(
                wallet=wallet, kind=TransactionKind.STORNO, amount=Decimal("5"), storno_of=original
            )


def test_multiple_transactions_without_storno_of_are_allowed(wallet):
    Transaction.objects.create(wallet=wallet, kind=TransactionKind.GRANT, amount=Decimal("1"))
    Transaction.objects.create(wallet=wallet, kind=TransactionKind.GRANT, amount=Decimal("2"))

    assert Transaction.objects.filter(storno_of__isnull=True).count() == 2


def test_deleting_original_transaction_is_protected_when_stornoed(wallet):
    original = Transaction.objects.create(wallet=wallet, kind=TransactionKind.CONSUMPTION, amount=Decimal("-5"))
    Transaction.objects.create(wallet=wallet, kind=TransactionKind.STORNO, amount=Decimal("5"), storno_of=original)

    with pytest.raises(Exception):
        with db_transaction.atomic():
            original.delete()


def test_attached_object_generic_foreign_key(wallet, subject):
    txn = Transaction.objects.create(wallet=wallet, kind=TransactionKind.CONSUMPTION, amount=Decimal("-1"))

    txn.attached_object = subject
    txn.save()
    txn.refresh_from_db()

    assert txn.attached_object == subject


def test_deleting_wallet_cascades_to_transactions(wallet):
    Transaction.objects.create(wallet=wallet, kind=TransactionKind.GRANT, amount=Decimal("1"))

    wallet.delete()

    assert Transaction.objects.count() == 0
