from decimal import Decimal

import pytest

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from django_licensing.models import Wallet, WalletType

pytestmark = pytest.mark.django_db


@pytest.fixture
def subject():
    return User.objects.create(username="alice")


def test_create_wallet_with_valid_fields(subject):
    wallet = Wallet.objects.create(
        subject=subject,
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        balance=Decimal("10.5"),
    )
    wallet.refresh_from_db()

    assert wallet.balance == Decimal("10.500000")
    assert wallet.wallet_type == WalletType.CONSUMABLE
    assert wallet.currency == "credits"
    assert wallet.valid_from is None
    assert wallet.valid_until is None
    assert wallet.overdraft_limit is None
    assert wallet.first_overdraft_at is None
    assert wallet.reset_config is None
    assert wallet.created_at is not None
    assert wallet.updated_at is not None


def test_wallet_subject_generic_foreign_key_resolves_correct_instance(subject):
    wallet = Wallet.objects.create(subject=subject, wallet_type=WalletType.GRANT, currency="email_send")

    wallet.refresh_from_db()

    assert wallet.subject == subject
    assert wallet.subject_content_type == ContentType.objects.get_for_model(User)
    assert wallet.subject_object_id == subject.pk


def test_wallet_balance_accepts_readme_example_decimal_values(subject):
    # README examples use conversion factors like 0.5 and 3.27 — balances must round-trip exactly.
    wallet = Wallet.objects.create(
        subject=subject, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("3.27")
    )
    wallet.refresh_from_db()

    assert wallet.balance == Decimal("3.270000")


def test_wallet_balance_can_be_negative(subject):
    wallet = Wallet.objects.create(
        subject=subject,
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        balance=Decimal("-2.5"),
        overdraft_limit=Decimal("10"),
    )
    wallet.refresh_from_db()

    assert wallet.balance == Decimal("-2.500000")
    assert wallet.overdraft_limit == Decimal("10.000000")


def test_wallet_validity_window_and_reset_config(subject):
    valid_from = timezone.now()
    valid_until = valid_from + timezone.timedelta(days=30)
    reset_config = {"day_of_month": 1, "time": "00:00:00"}

    wallet = Wallet.objects.create(
        subject=subject,
        wallet_type=WalletType.CONSUMABLE,
        currency="coolant_liters",
        valid_from=valid_from,
        valid_until=valid_until,
        reset_config=reset_config,
    )
    wallet.refresh_from_db()

    assert wallet.valid_from == valid_from
    assert wallet.valid_until == valid_until
    assert wallet.reset_config == reset_config


def test_mark_overdraft_if_needed_sets_timestamp_once(subject):
    wallet = Wallet.objects.create(
        subject=subject, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("-1")
    )

    assert wallet.first_overdraft_at is None
    changed = wallet.mark_overdraft_if_needed()
    assert changed is True
    assert wallet.first_overdraft_at is not None

    first_timestamp = wallet.first_overdraft_at
    changed_again = wallet.mark_overdraft_if_needed()
    assert changed_again is False
    assert wallet.first_overdraft_at == first_timestamp


def test_mark_overdraft_if_needed_noop_when_balance_not_negative(subject):
    wallet = Wallet.objects.create(
        subject=subject, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("5")
    )

    changed = wallet.mark_overdraft_if_needed()

    assert changed is False
    assert wallet.first_overdraft_at is None


def test_multiple_wallets_allowed_for_same_subject_and_currency(subject):
    # Grant wallet and consumable wallet in the same currency must be able to coexist.
    Wallet.objects.create(subject=subject, wallet_type=WalletType.GRANT, currency="email_send")
    Wallet.objects.create(subject=subject, wallet_type=WalletType.CONSUMABLE, currency="email_send")

    assert Wallet.objects.filter(subject_content_type=ContentType.objects.get_for_model(subject)).count() == 2


def test_wallet_str_contains_key_fields(subject):
    wallet = Wallet.objects.create(
        subject=subject, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("1")
    )

    text = str(wallet)

    assert "consumable" in text
    assert "credits" in text
