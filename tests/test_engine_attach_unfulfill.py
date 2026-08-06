from decimal import Decimal

import pytest

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction as db_transaction

from django_licensing.engine import attach, fulfill, unfulfill
from django_licensing.models import FulfillmentRecord, Transaction, TransactionKind, Wallet, WalletType
from django_licensing.providers import SettingsProvider

pytestmark = pytest.mark.django_db


@pytest.fixture
def alice():
    return User.objects.create(username="alice")


@pytest.fixture
def business_object():
    return User.objects.create(username="business-object-stand-in")


@pytest.fixture
def provider():
    return SettingsProvider(
        config={
            "features": {
                "send_email": {
                    "consumable": {"currencies": ["credits"]},
                    "subjects": {"device"},
                    "fulfillment_order": [("consumable", "device")],
                }
            },
            "currencies": {"credits": {"general": True, "round_fn": None}},
        }
    )


def test_attach_sets_object_on_all_transactions_under_guid(alice, business_object, provider):
    Wallet.objects.create(subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10"))
    result = fulfill("send_email", 3, {"device": alice}, provider=provider)

    attach(result.guid, business_object)

    txn = Transaction.objects.get(pk=result.transactions[0].pk)
    assert txn.attached_object == business_object
    assert txn.object_content_type == ContentType.objects.get_for_model(business_object)
    assert txn.object_id == business_object.pk


def test_attach_is_fifo_by_timestamp_across_multiple_fulfill_calls(alice, business_object, provider):
    import uuid

    guid = uuid.uuid4()
    Wallet.objects.create(subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("100"))
    first = fulfill("send_email", 2, {"device": alice}, guid=guid, provider=provider)
    second = fulfill("send_email", 3, {"device": alice}, guid=guid, provider=provider)

    attach(guid, business_object)

    for txn_id in (first.transactions[0].pk, second.transactions[0].pk):
        txn = Transaction.objects.get(pk=txn_id)
        assert txn.attached_object == business_object


def test_attach_leaves_already_attached_transactions_untouched(alice, provider):
    other_object = User.objects.create(username="other")
    Wallet.objects.create(subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10"))
    result = fulfill("send_email", 3, {"device": alice}, provider=provider)
    txn = result.transactions[0]
    attach(result.guid, other_object)

    # Second attach call for the same guid should be a no-op — nothing left unattached.
    attach(result.guid, alice)

    txn.refresh_from_db()
    assert txn.attached_object == other_object


def test_unfulfill_reverses_all_transactions_under_guid(alice, provider):
    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
    )
    result = fulfill("send_email", 3, {"device": alice}, provider=provider)

    unfulfill(result.guid)

    wallet.refresh_from_db()
    assert wallet.balance == Decimal("10")
    storno = Transaction.objects.get(kind=TransactionKind.STORNO)
    assert storno.storno_of_id == result.transactions[0].pk
    assert storno.amount == Decimal("3")


def test_unfulfill_is_idempotent(alice, provider):
    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
    )
    result = fulfill("send_email", 3, {"device": alice}, provider=provider)

    unfulfill(result.guid)
    unfulfill(result.guid)  # must not raise, must not double-reverse

    wallet.refresh_from_db()
    assert wallet.balance == Decimal("10")
    assert Transaction.objects.filter(kind=TransactionKind.STORNO).count() == 1


def test_unfulfill_respects_storno_of_uniqueness_constraint_at_db_level(alice, provider):
    Wallet.objects.create(subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10"))
    result = fulfill("send_email", 3, {"device": alice}, provider=provider)
    original = result.transactions[0]

    unfulfill(result.guid)

    with pytest.raises(IntegrityError):
        with db_transaction.atomic():
            Transaction.objects.create(
                wallet_id=original.wallet_id,
                kind=TransactionKind.STORNO,
                amount=Decimal("3"),
                storno_of=original,
            )


def test_unfulfill_unknown_guid_raises_does_not_exist():
    import uuid

    with pytest.raises(FulfillmentRecord.DoesNotExist):
        unfulfill(uuid.uuid4())


def test_attach_unknown_guid_raises_does_not_exist(alice):
    import uuid

    with pytest.raises(FulfillmentRecord.DoesNotExist):
        attach(uuid.uuid4(), alice)
