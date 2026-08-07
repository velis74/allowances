"""``plan_switch`` — closing one set of wallets and opening another, atomically."""

from datetime import timedelta
from decimal import Decimal

import pytest

from allowances.engine import plan_switch
from allowances.types import WalletSpec, WalletType
from allowances.windows import utcnow
from tests.conftest import open_wallet
from tests.helpers import wallets_for_subject


def test_closes_the_old_wallet_and_opens_the_new_one(store, alice):
    old = open_wallet(store, alice, balance="5")

    plan_switch(store, {"user": alice}, [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("100"))])

    closed = store.get_wallet(old.id)
    assert closed.valid_until is not None
    opened = [w for w in wallets_for_subject(store, alice) if w.id != old.id]
    assert len(opened) == 1
    assert opened[0].balance == Decimal("100")
    assert opened[0].valid_from == closed.valid_until


def test_a_future_effective_date_schedules_the_switch_rather_than_applying_it(store, alice):
    old = open_wallet(store, alice, balance="5")
    effective_date = utcnow() + timedelta(days=7)

    plan_switch(
        store,
        {"user": alice},
        [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("50"))],
        effective_date=effective_date,
    )

    closed = store.get_wallet(old.id)
    assert closed.valid_until == effective_date
    assert closed.valid_until > utcnow()  # still valid right now
    opened = [w for w in wallets_for_subject(store, alice) if w.id != old.id][0]
    assert opened.valid_from == effective_date


def test_every_spec_is_opened_once_per_subject(store, alice, bob):
    plan_switch(
        store,
        {"alice": alice, "bob": bob},
        [
            WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("10")),
            WalletSpec(WalletType.GRANT, "email_send", Decimal("0")),
        ],
    )

    assert len(wallets_for_subject(store, alice)) == 2
    assert len(wallets_for_subject(store, bob)) == 2


def test_returns_the_wallets_it_opened(store, alice):
    opened = plan_switch(
        store,
        {"user": alice},
        [
            WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("10")),
            WalletSpec(WalletType.GRANT, "email_send", Decimal("1")),
        ],
    )

    assert [w.currency for w in opened] == ["credits", "email_send"]
    assert [w.balance for w in opened] == [Decimal("10"), Decimal("1")]


def test_wallet_spec_details_are_carried_across(store, alice):
    until = utcnow() + timedelta(days=30)

    opened = plan_switch(
        store,
        {"user": alice},
        [
            WalletSpec(
                WalletType.CONSUMABLE,
                "credits",
                Decimal("10"),
                overdraft_limit=Decimal("5"),
                reset_config={"time": "00:00:00"},
                valid_until=until,
            )
        ],
    )

    assert opened[0].overdraft_limit == Decimal("5")
    assert opened[0].reset_config == {"time": "00:00:00"}
    assert opened[0].valid_until == until


def test_already_closed_wallets_keep_their_original_end_date(store, alice):
    now = utcnow()
    yesterday = now - timedelta(days=1)
    already_closed = open_wallet(store, alice, balance="5", valid_until=yesterday)

    plan_switch(store, {"user": alice}, [WalletSpec(WalletType.CONSUMABLE, "credits")], effective_date=now)

    assert store.get_wallet(already_closed.id).valid_until == yesterday


def test_a_failure_partway_through_rolls_the_whole_switch_back(store, alice, monkeypatch):
    old = open_wallet(store, alice, balance="5")
    original_create = store.create_wallet
    calls = {"n": 0}

    def flaky_create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure opening the second wallet")
        return original_create(**kwargs)

    monkeypatch.setattr(store, "create_wallet", flaky_create)

    with pytest.raises(RuntimeError):
        plan_switch(
            store,
            {"user": alice},
            [
                WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("1")),
                WalletSpec(WalletType.GRANT, "email_send", Decimal("2")),
            ],
        )

    # The old wallet must still be open and no new wallet may survive.
    assert store.get_wallet(old.id).valid_until is None
    assert [w.id for w in wallets_for_subject(store, alice)] == [old.id]
