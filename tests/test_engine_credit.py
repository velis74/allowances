"""``credit`` and ``open_wallet`` — funding wallets outside a plan switch."""

import uuid

from datetime import timedelta
from decimal import Decimal

import pytest

from allowances.engine import (
    adjust_wallet,
    consumed,
    credit,
    find_unattached_fulfillment_records,
    fulfill,
    open_wallet,
    plan_switch,
    unfulfill,
)
from allowances.errors import UnsuitableWalletError, WalletNotFoundError
from allowances.types import ObjectRef, SwitchPolicy, TransactionKind, WalletSpec, WalletType
from allowances.windows import utcnow
from tests.conftest import open_wallet as seed_wallet
from tests.helpers import transactions_for_wallet


def test_credit_raises_the_balance_and_writes_a_grant_entry(store, alice):
    wallet = seed_wallet(store, alice, balance="10")

    txn = credit(store, wallet.id, "5")

    assert store.get_wallet(wallet.id).balance == Decimal("15")
    assert txn.kind == TransactionKind.GRANT
    assert txn.amount == Decimal("5")
    assert [t.kind for t in transactions_for_wallet(store, wallet.id)] == [TransactionKind.GRANT]


def test_credit_attaches_the_paying_object(store, alice):
    wallet = seed_wallet(store, alice, balance="0")
    payment = ObjectRef("shop.payment", 7)

    txn = credit(store, wallet.id, "100", obj=payment)

    assert txn.obj == payment
    assert transactions_for_wallet(store, wallet.id)[0].obj == payment


def test_a_credit_under_a_guid_is_reversed_by_unfulfill(store, alice):
    wallet = seed_wallet(store, alice, balance="0")
    guid = uuid.uuid4()

    credit(store, wallet.id, "100", guid=guid, obj=ObjectRef("shop.payment", 7))
    unfulfill(store, guid)

    assert store.get_wallet(wallet.id).balance == Decimal("0")
    kinds = [t.kind for t in transactions_for_wallet(store, wallet.id)]
    assert kinds == [TransactionKind.GRANT, TransactionKind.STORNO]


def test_an_attached_credit_is_not_reported_as_unattached(store, alice):
    wallet = seed_wallet(store, alice, balance="0")

    credit(store, wallet.id, "100", guid=uuid.uuid4(), obj=ObjectRef("shop.payment", 7))

    assert find_unattached_fulfillment_records(store) == []


def test_a_credit_without_a_guid_creates_no_record(store, alice):
    wallet = seed_wallet(store, alice, balance="0")

    txn = credit(store, wallet.id, "100")

    assert txn.record_guid is None
    assert find_unattached_fulfillment_records(store) == []


@pytest.mark.parametrize("amount", ["0", "-1"])
def test_credit_refuses_a_non_positive_amount(store, alice, amount):
    wallet = seed_wallet(store, alice, balance="10")

    with pytest.raises(ValueError):
        credit(store, wallet.id, amount)


def test_credit_refuses_a_windowed_wallet(store, alice):
    wallet = seed_wallet(store, alice, balance="10", reset_config={"time": "00:00:00"})

    with pytest.raises(UnsuitableWalletError):
        credit(store, wallet.id, "5")


def test_credit_refuses_an_expired_wallet(store, alice):
    wallet = seed_wallet(store, alice, balance="10", valid_until=utcnow() - timedelta(days=1))

    with pytest.raises(UnsuitableWalletError):
        credit(store, wallet.id, "5")
    assert store.get_wallet(wallet.id).balance == Decimal("10")


def test_credit_refuses_an_unknown_wallet(store):
    with pytest.raises(WalletNotFoundError):
        credit(store, 999_999, "5")


def test_open_wallet_records_its_source(store, alice):
    wallet = open_wallet(store, alice, WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("50")), source="purchase")

    assert wallet.source == "purchase"
    assert wallet.balance == Decimal("50")
    assert wallet.allotment is None
    assert store.get_wallet(wallet.id).source == "purchase"


def test_a_purchased_wallet_survives_a_plan_switch(store, alice):
    bought = open_wallet(store, alice, WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("50")), source="purchase")
    planned = seed_wallet(store, alice, balance="10")

    plan_switch(store, {"user": alice}, [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("100"))])

    assert store.get_wallet(bought.id).valid_until is None
    assert store.get_wallet(planned.id).valid_until is not None


def test_a_drained_purchased_wallet_is_archived(store, alice, provider):
    bought = open_wallet(store, alice, WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("5")), source="purchase")

    fulfill(store, "send_email", 5, {"device": alice}, provider)

    assert store.get_wallet(bought.id) is None


# -- budgets: adjust_wallet and consumed ---------------------------------------------------------


def _budget(store, subject, limit, effective_date=None, **kwargs):
    spec = WalletSpec(WalletType.CONSUMABLE, "credits", Decimal(limit), on_switch=SwitchPolicy.ADJUST, **kwargs)
    return open_wallet(store, subject, spec, source="budget", effective_date=effective_date)


def test_adjust_wallet_moves_a_stored_balance_by_the_difference(store, alice, provider):
    budget = _budget(store, alice, "50")
    fulfill(store, "send_email", 20, {"device": alice}, provider)

    adjusted = adjust_wallet(store, budget.id, "80")

    assert adjusted.balance == Decimal("60")
    assert store.get_wallet(budget.id).allotment == Decimal("80")


def test_adjust_wallet_can_lower_a_limit(store, alice, provider):
    budget = _budget(store, alice, "50")
    fulfill(store, "send_email", 20, {"device": alice}, provider)

    adjust_wallet(store, budget.id, "10")

    assert store.get_wallet(budget.id).balance == Decimal("-10")


def test_adjust_wallet_on_a_windowed_wallet_sets_the_allotment(store, alice):
    budget = _budget(store, alice, "50", reset_config={"day_of_month": 1, "time": "00:00:00"})

    adjust_wallet(store, budget.id, "80")

    assert store.get_wallet(budget.id).balance == Decimal("80")


def test_adjust_wallet_refuses_a_wallet_without_an_allotment(store, alice):
    wallet = seed_wallet(store, alice, balance="10")

    with pytest.raises(UnsuitableWalletError):
        adjust_wallet(store, wallet.id, "20")


def test_adjust_wallet_refuses_an_unknown_wallet(store):
    with pytest.raises(WalletNotFoundError):
        adjust_wallet(store, 999_999, "20")


def test_consumed_nets_out_reversals_and_ignores_credits(store, alice, provider):
    start = utcnow() - timedelta(seconds=1)
    budget = _budget(store, alice, "50")
    fulfill(store, "send_email", 20, {"device": alice}, provider)
    refunded = fulfill(store, "send_email", 5, {"device": alice}, provider)
    unfulfill(store, refunded.guid)
    credit(store, budget.id, "30")
    adjust_wallet(store, budget.id, "60")

    assert consumed(store, budget.id, start, utcnow()) == Decimal("20")


def test_consumed_counts_only_the_interval(store, alice, provider):
    now = utcnow()
    budget = _budget(store, alice, "50", effective_date=now - timedelta(days=60))
    fulfill(store, "send_email", 20, {"device": alice}, provider, at=now - timedelta(days=40))
    fulfill(store, "send_email", 5, {"device": alice}, provider, at=now)

    assert consumed(store, budget.id, now - timedelta(days=30), now) == Decimal("5")
