"""Plan switches with a future effective date: applied on their date, cancellable before it."""

from datetime import timedelta
from decimal import Decimal

import pytest

from allowances import engine
from allowances.engine import (
    archive_expired_wallets,
    cancel_scheduled_switch,
    credit,
    effective_balance,
    fulfill,
    peek,
    plan_switch,
)
from allowances.errors import UnsuitableWalletError
from allowances.types import SwitchPolicy, TransactionKind, WalletSpec, WalletType
from allowances.windows import utcnow
from tests.conftest import open_wallet
from tests.helpers import transactions_for_wallet, wallets_for_subject


def _spec(balance, policy=SwitchPolicy.REPLACE, **kwargs):
    return WalletSpec(WalletType.CONSUMABLE, "credits", Decimal(balance), on_switch=policy, **kwargs)


@pytest.fixture
def clock(monkeypatch):
    """Moves the engine's notion of "now"."""
    state = {"now": utcnow()}
    monkeypatch.setattr(engine, "utcnow", lambda: state["now"])
    return state


def test_a_scheduled_top_up_changes_nothing_before_its_date(store, alice, provider):
    [wallet] = plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])

    plan_switch(
        store, {"user": alice}, [_spec("300", SwitchPolicy.TOP_UP)], effective_date=utcnow() + timedelta(days=7)
    )

    assert peek(store, "send_email", 101, {"device": alice}, provider).satisfied is False
    assert store.get_wallet(wallet.id).balance == Decimal("100")


def test_a_scheduled_top_up_applies_on_its_date(store, alice, provider):
    [wallet] = plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])
    when = utcnow() + timedelta(days=7)
    plan_switch(store, {"user": alice}, [_spec("300", SwitchPolicy.TOP_UP)], effective_date=when)
    later = when + timedelta(hours=1)

    assert peek(store, "send_email", 300, {"device": alice}, provider, at=later).satisfied is True
    assert effective_balance(store, store.get_wallet(wallet.id), later) == Decimal("300")

    fulfill(store, "send_email", 10, {"device": alice}, provider, at=later)

    stored = store.get_wallet(wallet.id)
    assert (stored.balance, stored.allotment, stored.scheduled) == (Decimal("290"), Decimal("300"), None)
    grants = [t for t in transactions_for_wallet(store, wallet.id) if t.kind == TransactionKind.GRANT]
    assert [(t.amount, t.created_at) for t in grants] == [(Decimal("200"), when)]


def test_a_scheduled_downgrade_counts_usage_until_its_date(store, alice, provider):
    [wallet] = plan_switch(store, {"user": alice}, [_spec("20", SwitchPolicy.ADJUST)])
    when = utcnow() + timedelta(days=7)
    plan_switch(store, {"user": alice}, [_spec("10", SwitchPolicy.ADJUST)], effective_date=when)

    fulfill(store, "send_email", 5, {"device": alice}, provider)

    assert effective_balance(store, store.get_wallet(wallet.id), when + timedelta(days=1)) == Decimal("5")


def test_a_scheduled_adjustment_of_a_windowed_wallet_changes_its_allotment_on_the_date(store, alice):
    daily = {"time": "00:00:00"}
    [wallet] = plan_switch(store, {"user": alice}, [_spec("200", SwitchPolicy.ADJUST, reset_config=daily)])
    when = utcnow() + timedelta(days=7)

    plan_switch(store, {"user": alice}, [_spec("50", SwitchPolicy.ADJUST, reset_config=daily)], effective_date=when)

    stored = store.get_wallet(wallet.id)
    assert effective_balance(store, stored, when - timedelta(seconds=1)) == Decimal("200")
    assert effective_balance(store, stored, when) == Decimal("50")


def test_cancel_restores_closed_wallets_and_voids_opened_ones(store, alice):
    until = utcnow() + timedelta(days=30)
    old = open_wallet(store, alice, balance="5", valid_until=until)
    when = utcnow() + timedelta(days=7)
    [new] = plan_switch(store, {"user": alice}, [_spec("50")], effective_date=when)
    assert store.get_wallet(old.id).valid_until == when

    assert cancel_scheduled_switch(store, {"user": alice}) == 1

    restored = store.get_wallet(old.id)
    assert (restored.valid_until, restored.scheduled) == (until, None)
    voided = store.get_wallet(new.id)
    assert not voided.is_valid_at(when) and voided.scheduled is None
    assert cancel_scheduled_switch(store, {"user": alice}) == 0


def test_cancel_drops_a_scheduled_adjustment(store, alice):
    [wallet] = plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])
    when = utcnow() + timedelta(days=7)
    plan_switch(store, {"user": alice}, [_spec("300", SwitchPolicy.TOP_UP)], effective_date=when)

    cancel_scheduled_switch(store, {"user": alice})

    assert effective_balance(store, store.get_wallet(wallet.id), when + timedelta(days=1)) == Decimal("100")


def test_a_voided_wallet_is_archived_by_the_sweep(store, alice):
    [new] = plan_switch(store, {"user": alice}, [_spec("50")], effective_date=utcnow() + timedelta(days=7))
    cancel_scheduled_switch(store, {"user": alice})

    archive_expired_wallets(store)

    assert store.get_wallet(new.id) is None


def test_a_switch_that_has_taken_effect_cannot_be_cancelled(store, alice, clock):
    old = open_wallet(store, alice, balance="5")
    when = clock["now"] + timedelta(days=7)
    plan_switch(store, {"user": alice}, [_spec("50")], effective_date=when)

    clock["now"] = when + timedelta(seconds=1)

    assert cancel_scheduled_switch(store, {"user": alice}) == 0
    assert store.get_wallet(old.id).valid_until == when


def test_a_new_switch_replaces_the_scheduled_one(store, alice):
    old = open_wallet(store, alice, balance="5")
    first = utcnow() + timedelta(days=7)
    [abandoned] = plan_switch(store, {"user": alice}, [_spec("50")], effective_date=first)

    second = utcnow() + timedelta(days=14)
    [new] = plan_switch(store, {"user": alice}, [_spec("80")], effective_date=second)

    assert store.get_wallet(old.id).valid_until == second
    assert not store.get_wallet(abandoned.id).is_valid_at(first)
    assert new.valid_from == second
    assert len([w for w in wallets_for_subject(store, alice) if w.is_valid_at(second + timedelta(seconds=1))]) == 1


def test_an_immediate_switch_replaces_the_scheduled_one(store, alice):
    [wallet] = plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])
    when = utcnow() + timedelta(days=7)
    plan_switch(store, {"user": alice}, [_spec("300", SwitchPolicy.TOP_UP)], effective_date=when)

    plan_switch(store, {"user": alice}, [_spec("150", SwitchPolicy.TOP_UP)])

    stored = store.get_wallet(wallet.id)
    assert (stored.balance, stored.scheduled) == (Decimal("150"), None)
    assert effective_balance(store, stored, when + timedelta(days=1)) == Decimal("150")


def test_a_scheduled_switch_is_settled_when_the_next_one_arrives(store, alice, clock):
    [wallet] = plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])
    when = clock["now"] + timedelta(days=7)
    plan_switch(store, {"user": alice}, [_spec("300", SwitchPolicy.TOP_UP)], effective_date=when)

    clock["now"] = when + timedelta(days=1)
    plan_switch(store, {"user": alice}, [_spec("400", SwitchPolicy.TOP_UP)])

    assert store.get_wallet(wallet.id).balance == Decimal("400")


def test_credit_refuses_a_wallet_that_is_not_yet_valid(store, alice):
    [new] = plan_switch(store, {"user": alice}, [_spec("50")], effective_date=utcnow() + timedelta(days=7))

    with pytest.raises(UnsuitableWalletError):
        credit(store, new.id, "5")
