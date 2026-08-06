from decimal import Decimal

import pytest

from django.contrib.auth.models import User
from django.utils import timezone

from django_licensing.engine import archive_expired_wallets, fulfill
from django_licensing.models import Transaction, TransactionArchive, Wallet, WalletArchive, WalletType
from django_licensing.providers import SettingsProvider

pytestmark = pytest.mark.django_db


@pytest.fixture
def alice():
    return User.objects.create(username="alice")


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


def test_fulfill_archives_wallet_drained_to_zero_with_no_overdraft(alice, provider):
    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("5")
    )
    wallet_id = wallet.id

    fulfill("send_email", 5, {"device": alice}, provider=provider)

    assert not Wallet.objects.filter(pk=wallet_id).exists()
    archive = WalletArchive.objects.get(original_wallet_id=wallet_id)
    assert archive.final_balance == Decimal("0")
    assert archive.currency == "credits"
    txn_archive = TransactionArchive.objects.get(original_wallet_id=wallet_id)
    assert txn_archive.amount == Decimal("-5")
    assert txn_archive.wallet_archive_id == archive.id


def test_fulfill_does_not_archive_wallet_left_with_positive_balance(alice, provider):
    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
    )

    fulfill("send_email", 5, {"device": alice}, provider=provider)

    assert Wallet.objects.filter(pk=wallet.pk).exists()
    assert WalletArchive.objects.count() == 0


def test_fulfill_does_not_archive_drained_wallet_that_still_has_overdraft_capacity(alice):
    wallet = Wallet.objects.create(
        subject=alice,
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        balance=Decimal("5"),
        overdraft_limit=Decimal("10"),
    )
    provider = SettingsProvider(
        config={
            "features": {
                "send_email": {
                    "consumable": {"currencies": ["credits"]},
                    "subjects": {"device"},
                    "fulfillment_order": [("consumable", "device")],
                    "overdraft": True,
                }
            },
            "currencies": {"credits": {"general": True, "round_fn": None}},
        }
    )

    fulfill("send_email", 5, {"device": alice}, provider=provider)

    assert Wallet.objects.filter(pk=wallet.pk).exists()
    assert WalletArchive.objects.count() == 0


def test_archive_expired_wallets_moves_untouched_expired_wallet(alice):
    now = timezone.now()
    wallet = Wallet.objects.create(
        subject=alice,
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        balance=Decimal("7"),
        valid_until=now - timezone.timedelta(days=1),
    )
    wallet_id = wallet.id

    archived_count = archive_expired_wallets(at=now)

    assert archived_count == 1
    assert not Wallet.objects.filter(pk=wallet_id).exists()
    archive = WalletArchive.objects.get(original_wallet_id=wallet_id)
    assert archive.final_balance == Decimal("7")


def test_archive_expired_wallets_leaves_active_wallets_alone(alice):
    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("7")
    )

    archived_count = archive_expired_wallets()

    assert archived_count == 0
    assert Wallet.objects.filter(pk=wallet.pk).exists()


def test_archive_preserves_storno_of_link_between_archived_transactions(alice, provider):
    from django_licensing.engine import unfulfill

    # balance stays positive throughout, so the wallet is never auto-archived by the "drained"
    # trigger — it only gets archived once explicitly expired below.
    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
    )
    wallet_id = wallet.id
    result = fulfill("send_email", 3, {"device": alice}, provider=provider)
    unfulfill(result.guid)
    wallet.refresh_from_db()
    assert wallet.balance == Decimal("10")
    assert Transaction.objects.filter(wallet=wallet).count() == 2

    now = timezone.now()
    wallet.valid_until = now - timezone.timedelta(seconds=1)
    wallet.save()

    archived_count = archive_expired_wallets(at=now)

    assert archived_count == 1
    archive = WalletArchive.objects.get(original_wallet_id=wallet_id)
    original_archive = TransactionArchive.objects.get(wallet_archive=archive, storno_of__isnull=True)
    storno_archive = TransactionArchive.objects.get(wallet_archive=archive, storno_of__isnull=False)
    assert storno_archive.storno_of_id == original_archive.id
    assert storno_archive.amount == Decimal("3")
    assert original_archive.amount == Decimal("-3")
