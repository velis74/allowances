from decimal import Decimal

import pytest

from django.contrib.auth.models import User

from django_licensing.engine import fulfill, peek
from django_licensing.models import Transaction, Wallet, WalletType
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


def test_peek_does_not_change_wallet_balance_or_create_transactions(alice, provider):
    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
    )

    result = peek("send_email", 4, {"device": alice}, provider=provider)

    wallet.refresh_from_db()
    assert wallet.balance == Decimal("10")
    assert Transaction.objects.count() == 0
    assert result.transactions == []
    assert result.guid is None
    assert result.granted == Decimal("4")
    assert result.satisfied is True


def test_peek_reports_partial_availability_without_persisting(alice, provider):
    Wallet.objects.create(subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("3"))

    result = peek("send_email", 10, {"device": alice}, provider=provider)

    assert result.granted == Decimal("3")
    assert result.satisfied is False
    assert Transaction.objects.count() == 0


def test_peek_matches_what_fulfill_would_actually_grant(alice, provider):
    Wallet.objects.create(subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("6"))

    peeked = peek("send_email", 5, {"device": alice}, provider=provider)
    fulfilled = fulfill("send_email", 5, {"device": alice}, provider=provider)

    assert peeked.granted == fulfilled.granted
    assert peeked.satisfied == fulfilled.satisfied
