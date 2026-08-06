from decimal import Decimal

import pytest

from django.contrib.auth.models import User
from django.utils import timezone

from django_licensing.engine import plan_switch, WalletSpec
from django_licensing.models import Wallet, WalletType

pytestmark = pytest.mark.django_db


@pytest.fixture
def alice():
    return User.objects.create(username="alice")


@pytest.fixture
def bob():
    return User.objects.create(username="bob")


def test_plan_switch_immediate_closes_old_and_opens_new(alice):
    old = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("5")
    )

    plan_switch(
        {"user": alice},
        [WalletSpec(wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("100"))],
    )

    old.refresh_from_db()
    assert old.valid_until is not None

    new_wallets = Wallet.objects.filter(subject_object_id=alice.pk).exclude(pk=old.pk)
    assert new_wallets.count() == 1
    new_wallet = new_wallets.get()
    assert new_wallet.balance == Decimal("100")
    assert new_wallet.valid_from == old.valid_until


def test_plan_switch_with_future_effective_date_sets_matching_valid_from_and_until(alice):
    old = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("5")
    )
    effective_date = timezone.now() + timezone.timedelta(days=7)

    plan_switch(
        {"user": alice},
        [WalletSpec(wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("50"))],
        effective_date=effective_date,
    )

    old.refresh_from_db()
    assert old.valid_until == effective_date

    new_wallet = Wallet.objects.exclude(pk=old.pk).get(subject_object_id=alice.pk)
    assert new_wallet.valid_from == effective_date
    # The old wallet is still valid right now — plan_switch scheduled a future close, not an
    # immediate one.
    assert old.valid_until > timezone.now()


def test_plan_switch_opens_one_set_of_wallets_per_subject(alice, bob):
    plan_switch(
        {"alice": alice, "bob": bob},
        [
            WalletSpec(wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")),
            WalletSpec(wallet_type=WalletType.GRANT, currency="email_send", balance=Decimal("0")),
        ],
    )

    assert Wallet.objects.filter(subject_object_id=alice.pk).count() == 2
    assert Wallet.objects.filter(subject_object_id=bob.pk).count() == 2


def test_plan_switch_does_not_touch_already_closed_wallets(alice):
    now = timezone.now()
    already_closed = Wallet.objects.create(
        subject=alice,
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        balance=Decimal("5"),
        valid_until=now - timezone.timedelta(days=1),
    )

    plan_switch(
        {"user": alice}, [WalletSpec(wallet_type=WalletType.CONSUMABLE, currency="credits")], effective_date=now
    )

    already_closed.refresh_from_db()
    assert already_closed.valid_until == now - timezone.timedelta(days=1)


def test_plan_switch_is_atomic(alice, monkeypatch):
    from django_licensing import engine

    old = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("5")
    )

    original_create = Wallet.objects.create
    call_count = {"n": 0}

    def flaky_create(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated failure opening the second wallet")
        return original_create(*args, **kwargs)

    monkeypatch.setattr(Wallet.objects, "create", flaky_create)

    with pytest.raises(RuntimeError):
        engine.plan_switch(
            {"user": alice},
            [
                WalletSpec(wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("1")),
                WalletSpec(wallet_type=WalletType.GRANT, currency="email_send", balance=Decimal("2")),
            ],
        )

    old.refresh_from_db()
    # The whole operation must have rolled back: old wallet still open, no new wallet created.
    assert old.valid_until is None
    assert Wallet.objects.exclude(pk=old.pk).count() == 0
