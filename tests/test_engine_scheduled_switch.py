"""Plan switches with a future effective date: applied on their date, cancellable before it."""

from datetime import timedelta
from decimal import Decimal

import pytest

from allowances import engine
from allowances.errors import UnsuitableWalletError
from allowances.types import SwitchPolicy, TransactionKind, WalletSpec, WalletType
from allowances.windows import utcnow
from tests.api import (
    archive_expired_wallets,
    cancel_scheduled_switch,
    credit,
    effective_balance,
    fulfill,
    peek,
    plan_switch,
)
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


async def test_a_scheduled_top_up_changes_nothing_before_its_date(store, alice, provider):
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])

    await plan_switch(
        store, {"user": alice}, [_spec("300", SwitchPolicy.TOP_UP)], effective_date=utcnow() + timedelta(days=7)
    )

    assert (await peek(store, "send_email", 101, {"device": alice}, provider)).satisfied is False
    assert (await store.get_wallet(wallet.id)).balance == Decimal("100")


async def test_a_scheduled_top_up_applies_on_its_date(store, alice, provider):
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])
    when = utcnow() + timedelta(days=7)
    await plan_switch(store, {"user": alice}, [_spec("300", SwitchPolicy.TOP_UP)], effective_date=when)
    later = when + timedelta(hours=1)

    assert (await peek(store, "send_email", 300, {"device": alice}, provider, at=later)).satisfied is True
    assert await effective_balance(store, await store.get_wallet(wallet.id), later) == Decimal("300")

    await fulfill(store, "send_email", 10, {"device": alice}, provider, at=later)

    stored = await store.get_wallet(wallet.id)
    assert (stored.balance, stored.allotment, stored.scheduled) == (Decimal("290"), Decimal("300"), None)
    grants = [t for t in await transactions_for_wallet(store, wallet.id) if t.kind == TransactionKind.GRANT]
    assert [(t.amount, t.created_at) for t in grants] == [(Decimal("200"), when)]


async def test_a_scheduled_downgrade_counts_usage_until_its_date(store, alice, provider):
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("20", SwitchPolicy.ADJUST)])
    when = utcnow() + timedelta(days=7)
    await plan_switch(store, {"user": alice}, [_spec("10", SwitchPolicy.ADJUST)], effective_date=when)

    await fulfill(store, "send_email", 5, {"device": alice}, provider)

    assert await effective_balance(store, await store.get_wallet(wallet.id), when + timedelta(days=1)) == Decimal("5")


async def test_a_scheduled_adjustment_of_a_windowed_wallet_changes_its_allotment_on_the_date(store, alice):
    daily = {"time": "00:00:00"}
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("200", SwitchPolicy.ADJUST, reset_config=daily)])
    when = utcnow() + timedelta(days=7)

    await plan_switch(
        store, {"user": alice}, [_spec("50", SwitchPolicy.ADJUST, reset_config=daily)], effective_date=when
    )

    stored = await store.get_wallet(wallet.id)
    assert await effective_balance(store, stored, when - timedelta(seconds=1)) == Decimal("200")
    assert await effective_balance(store, stored, when) == Decimal("50")


async def test_cancel_restores_closed_wallets_and_voids_opened_ones(store, alice):
    until = utcnow() + timedelta(days=30)
    old = await open_wallet(store, alice, balance="5", valid_until=until)
    when = utcnow() + timedelta(days=7)
    [new] = await plan_switch(store, {"user": alice}, [_spec("50")], effective_date=when)
    assert (await store.get_wallet(old.id)).valid_until == when

    assert await cancel_scheduled_switch(store, {"user": alice}) == 1

    restored = await store.get_wallet(old.id)
    assert (restored.valid_until, restored.scheduled) == (until, None)
    voided = await store.get_wallet(new.id)
    assert not voided.is_valid_at(when) and voided.scheduled is None
    assert await cancel_scheduled_switch(store, {"user": alice}) == 0


async def test_cancel_drops_a_scheduled_adjustment(store, alice):
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])
    when = utcnow() + timedelta(days=7)
    await plan_switch(store, {"user": alice}, [_spec("300", SwitchPolicy.TOP_UP)], effective_date=when)

    await cancel_scheduled_switch(store, {"user": alice})

    assert await effective_balance(store, await store.get_wallet(wallet.id), when + timedelta(days=1)) == Decimal("100")


async def test_a_voided_wallet_is_archived_by_the_sweep(store, alice):
    [new] = await plan_switch(store, {"user": alice}, [_spec("50")], effective_date=utcnow() + timedelta(days=7))
    await cancel_scheduled_switch(store, {"user": alice})

    await archive_expired_wallets(store)

    assert await store.get_wallet(new.id) is None


async def test_a_switch_that_has_taken_effect_cannot_be_cancelled(store, alice, clock):
    old = await open_wallet(store, alice, balance="5")
    when = clock["now"] + timedelta(days=7)
    await plan_switch(store, {"user": alice}, [_spec("50")], effective_date=when)

    clock["now"] = when

    assert await cancel_scheduled_switch(store, {"user": alice}) == 0
    assert (await store.get_wallet(old.id)).valid_until == when


async def test_a_new_switch_replaces_the_scheduled_one(store, alice):
    old = await open_wallet(store, alice, balance="5")
    first = utcnow() + timedelta(days=7)
    [abandoned] = await plan_switch(store, {"user": alice}, [_spec("50")], effective_date=first)

    second = utcnow() + timedelta(days=14)
    [new] = await plan_switch(store, {"user": alice}, [_spec("80")], effective_date=second)

    assert (await store.get_wallet(old.id)).valid_until == second
    assert not (await store.get_wallet(abandoned.id)).is_valid_at(first)
    assert new.valid_from == second
    for at in (second - timedelta(microseconds=1), second):
        assert len([w for w in await wallets_for_subject(store, alice) if w.is_valid_at(at)]) == 1


async def test_an_immediate_switch_replaces_the_scheduled_one(store, alice):
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])
    when = utcnow() + timedelta(days=7)
    await plan_switch(store, {"user": alice}, [_spec("300", SwitchPolicy.TOP_UP)], effective_date=when)

    await plan_switch(store, {"user": alice}, [_spec("150", SwitchPolicy.TOP_UP)])

    stored = await store.get_wallet(wallet.id)
    assert (stored.balance, stored.scheduled) == (Decimal("150"), None)
    assert await effective_balance(store, stored, when + timedelta(days=1)) == Decimal("150")


async def test_a_scheduled_switch_is_settled_when_the_next_one_arrives(store, alice, clock):
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])
    when = clock["now"] + timedelta(days=7)
    await plan_switch(store, {"user": alice}, [_spec("300", SwitchPolicy.TOP_UP)], effective_date=when)

    clock["now"] = when + timedelta(days=1)
    await plan_switch(store, {"user": alice}, [_spec("400", SwitchPolicy.TOP_UP)])

    assert (await store.get_wallet(wallet.id)).balance == Decimal("400")


async def test_credit_refuses_a_wallet_that_is_not_yet_valid(store, alice):
    [new] = await plan_switch(store, {"user": alice}, [_spec("50")], effective_date=utcnow() + timedelta(days=7))

    with pytest.raises(UnsuitableWalletError):
        await credit(store, new.id, "5")


async def test_a_scheduled_switch_hands_over_at_its_effective_date(store, alice):
    old = await open_wallet(store, alice, balance="5")
    when = utcnow() + timedelta(days=7)
    [new] = await plan_switch(store, {"user": alice}, [_spec("50")], effective_date=when)

    async def valid(at):
        return [
            w.id
            for w in await store.find_wallets(
                subjects=[alice], wallet_types=[WalletType.CONSUMABLE], currencies=None, at=at
            )
        ]

    assert await valid(when - timedelta(microseconds=1)) == [old.id]
    assert await valid(when) == [new.id]
