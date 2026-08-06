from decimal import Decimal

import pytest

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from django_licensing.models import (
    FulfillmentRecord,
    TransactionArchive,
    TransactionKind,
    WalletArchive,
    WalletType,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def subject():
    return User.objects.create(username="dave")


def test_create_wallet_archive(subject):
    archive = WalletArchive.objects.create(
        original_wallet_id=42,
        subject=subject,
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        final_balance=Decimal("0"),
        original_created_at=timezone.now(),
    )
    archive.refresh_from_db()

    assert archive.original_wallet_id == 42
    assert archive.subject == subject
    assert archive.subject_content_type == ContentType.objects.get_for_model(User)
    assert archive.final_balance == Decimal("0.000000")
    assert archive.archived_at is not None


def test_wallet_archive_does_not_require_live_wallet_to_exist(subject):
    # original_wallet_id is a plain reference, not an FK — archiving after the live row is gone
    # (or referencing an id that was never a real Wallet row in this test) must still work.
    archive = WalletArchive.objects.create(
        original_wallet_id=999999,
        subject=subject,
        wallet_type=WalletType.GRANT,
        currency="email_send",
        final_balance=Decimal("0"),
        original_created_at=timezone.now(),
    )

    assert archive.pk is not None


def test_create_transaction_archive_linked_to_wallet_archive(subject):
    wallet_archive = WalletArchive.objects.create(
        original_wallet_id=1,
        subject=subject,
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        final_balance=Decimal("0"),
        original_created_at=timezone.now(),
    )
    record = FulfillmentRecord.objects.create()

    txn_archive = TransactionArchive.objects.create(
        original_transaction_id=7,
        wallet_archive=wallet_archive,
        original_wallet_id=1,
        fulfillment_record=record,
        kind=TransactionKind.CONSUMPTION,
        amount=Decimal("-1.5"),
        feature="send_email",
        original_created_at=timezone.now(),
    )
    txn_archive.refresh_from_db()

    assert txn_archive.wallet_archive_id == wallet_archive.id
    assert txn_archive.original_wallet_id == 1
    assert txn_archive.fulfillment_record_id == record.id
    assert txn_archive.amount == Decimal("-1.500000")
    assert txn_archive.archived_at is not None


def test_transaction_archive_wallet_archive_is_optional():
    # A transaction may be archived before/without its wallet being archived (original_wallet_id
    # keeps the link alive regardless).
    txn_archive = TransactionArchive.objects.create(
        original_transaction_id=8,
        original_wallet_id=123,
        kind=TransactionKind.GRANT,
        amount=Decimal("1"),
        original_created_at=timezone.now(),
    )

    assert txn_archive.wallet_archive is None
    assert txn_archive.original_wallet_id == 123


def test_transaction_archive_storno_of_self_fk():
    original = TransactionArchive.objects.create(
        original_transaction_id=1,
        original_wallet_id=1,
        kind=TransactionKind.CONSUMPTION,
        amount=Decimal("-5"),
        original_created_at=timezone.now(),
    )
    storno = TransactionArchive.objects.create(
        original_transaction_id=2,
        original_wallet_id=1,
        kind=TransactionKind.STORNO,
        amount=Decimal("5"),
        storno_of=original,
        original_created_at=timezone.now(),
    )

    assert storno.storno_of_id == original.id
    assert original.storno_transactions.get() == storno


def test_deleting_wallet_archive_cascades_to_transaction_archive(subject):
    wallet_archive = WalletArchive.objects.create(
        original_wallet_id=1,
        subject=subject,
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        final_balance=Decimal("0"),
        original_created_at=timezone.now(),
    )
    TransactionArchive.objects.create(
        original_transaction_id=1,
        wallet_archive=wallet_archive,
        original_wallet_id=1,
        kind=TransactionKind.GRANT,
        amount=Decimal("1"),
        original_created_at=timezone.now(),
    )

    wallet_archive.delete()

    assert TransactionArchive.objects.count() == 0
