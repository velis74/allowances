"""``plan_switch`` — closing one set of wallets and opening another, atomically."""

from datetime import timedelta
from decimal import Decimal

import pytest

from allowances.types import SwitchPolicy, TransactionKind, WalletSpec, WalletType
from allowances.windows import utcnow
from tests.api import fulfill, plan_switch, raw, unfulfill
from tests.conftest import open_wallet
from tests.helpers import transactions_for_wallet, wallets_for_subject


async def test_closes_the_old_wallet_and_opens_the_new_one(store, alice):
    old = await open_wallet(store, alice, balance="5")

    await plan_switch(store, {"user": alice}, [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("100"))])

    closed = await store.get_wallet(old.id)
    assert closed.valid_until is not None
    opened = [w for w in await wallets_for_subject(store, alice) if w.id != old.id]
    assert len(opened) == 1
    assert opened[0].balance == Decimal("100")
    assert opened[0].valid_from == closed.valid_until


async def test_a_future_effective_date_schedules_the_switch_rather_than_applying_it(store, alice):
    old = await open_wallet(store, alice, balance="5")
    effective_date = utcnow() + timedelta(days=7)

    await plan_switch(
        store,
        {"user": alice},
        [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("50"))],
        effective_date=effective_date,
    )

    closed = await store.get_wallet(old.id)
    assert closed.valid_until == effective_date
    assert closed.valid_until > utcnow()  # still valid right now
    opened = [w for w in await wallets_for_subject(store, alice) if w.id != old.id][0]
    assert opened.valid_from == effective_date


async def test_every_spec_is_opened_once_per_subject(store, alice, bob):
    await plan_switch(
        store,
        {"alice": alice, "bob": bob},
        [
            WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("10")),
            WalletSpec(WalletType.GRANT, "email_send", Decimal("0")),
        ],
    )

    assert len(await wallets_for_subject(store, alice)) == 2
    assert len(await wallets_for_subject(store, bob)) == 2


async def test_returns_the_wallets_it_opened(store, alice):
    opened = await plan_switch(
        store,
        {"user": alice},
        [
            WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("10")),
            WalletSpec(WalletType.GRANT, "email_send", Decimal("1")),
        ],
    )

    assert [w.currency for w in opened] == ["credits", "email_send"]
    assert [w.balance for w in opened] == [Decimal("10"), Decimal("1")]


async def test_wallet_spec_details_are_carried_across(store, alice):
    until = utcnow() + timedelta(days=30)

    opened = await plan_switch(
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


async def test_already_closed_wallets_keep_their_original_end_date(store, alice):
    now = utcnow()
    yesterday = now - timedelta(days=1)
    already_closed = await open_wallet(store, alice, balance="5", valid_until=yesterday)

    await plan_switch(store, {"user": alice}, [WalletSpec(WalletType.CONSUMABLE, "credits")], effective_date=now)

    assert (await store.get_wallet(already_closed.id)).valid_until == yesterday


async def test_a_failure_partway_through_rolls_the_whole_switch_back(store, alice, monkeypatch):
    old = await open_wallet(store, alice, balance="5")
    engine_store = raw(store)
    original_create = engine_store.create_wallet
    calls = {"n": 0}

    async def flaky_create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure opening the second wallet")
        return await original_create(**kwargs)

    monkeypatch.setattr(engine_store, "create_wallet", flaky_create)

    with pytest.raises(RuntimeError):
        await plan_switch(
            store,
            {"user": alice},
            [
                WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("1")),
                WalletSpec(WalletType.GRANT, "email_send", Decimal("2")),
            ],
        )

    # The old wallet must still be open and no new wallet may survive.
    assert (await store.get_wallet(old.id)).valid_until is None
    assert [w.id for w in await wallets_for_subject(store, alice)] == [old.id]


# -- switch policies ---------------------------------------------------------------------------


def _spec(balance, policy, **kwargs):
    return WalletSpec(WalletType.CONSUMABLE, "credits", Decimal(balance), on_switch=policy, **kwargs)


async def test_only_plan_wallets_are_closed(store, alice):
    plan = await open_wallet(store, alice, balance="5")
    budget = await open_wallet(store, alice, balance="50", source="budget")

    await plan_switch(store, {"user": alice}, [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("100"))])

    assert (await store.get_wallet(plan.id)).valid_until is not None
    assert (await store.get_wallet(budget.id)).valid_until is None


async def test_a_spec_with_a_keep_policy_records_its_allotment(store, alice):
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])

    assert wallet.allotment == Decimal("100")
    assert (await store.get_wallet(wallet.id)).allotment == Decimal("100")


async def test_a_replace_spec_records_no_allotment(store, alice):
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.REPLACE)])

    assert wallet.allotment is None


async def test_top_up_keeps_the_wallet_and_adds_the_difference(store, alice, provider):
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])
    await fulfill(store, "send_email", 70, {"device": alice}, provider)

    [after] = await plan_switch(store, {"user": alice}, [_spec("300", SwitchPolicy.TOP_UP)])

    assert after.id == wallet.id
    assert (await store.get_wallet(wallet.id)).balance == Decimal("230")
    assert (await store.get_wallet(wallet.id)).valid_until is None
    grants = [t for t in await transactions_for_wallet(store, wallet.id) if t.kind == TransactionKind.GRANT]
    assert [t.amount for t in grants] == [Decimal("200")]


async def test_top_up_takes_nothing_away_on_a_downgrade(store, alice, provider):
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("300", SwitchPolicy.TOP_UP)])
    await fulfill(store, "send_email", 50, {"device": alice}, provider)

    await plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])

    stored = await store.get_wallet(wallet.id)
    assert stored.balance == Decimal("250")
    assert stored.allotment == Decimal("100")


async def test_an_upgrade_after_a_downgrade_adds_the_difference_against_the_downgraded_plan(store, alice):
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("300", SwitchPolicy.TOP_UP)])
    await plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])

    await plan_switch(store, {"user": alice}, [_spec("300", SwitchPolicy.TOP_UP)])

    assert (await store.get_wallet(wallet.id)).balance == Decimal("500")


async def test_a_drained_top_up_wallet_stays_live_and_receives_the_difference(store, alice, provider):
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])
    await fulfill(store, "send_email", 100, {"device": alice}, provider)

    await plan_switch(store, {"user": alice}, [_spec("300", SwitchPolicy.TOP_UP)])

    assert (await store.get_wallet(wallet.id)).balance == Decimal("200")


async def test_adjust_carries_usage_across_an_upgrade(store, alice, provider):
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("10", SwitchPolicy.ADJUST)])
    await fulfill(store, "send_email", 8, {"device": alice}, provider)

    await plan_switch(store, {"user": alice}, [_spec("20", SwitchPolicy.ADJUST)])

    stored = await store.get_wallet(wallet.id)
    assert stored.balance == Decimal("12")
    assert stored.allotment == Decimal("20")


async def test_adjust_can_leave_a_downgraded_wallet_over_its_limit(store, alice, provider):
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("20", SwitchPolicy.ADJUST)])
    await fulfill(store, "send_email", 15, {"device": alice}, provider)

    await plan_switch(store, {"user": alice}, [_spec("10", SwitchPolicy.ADJUST)])

    assert (await store.get_wallet(wallet.id)).balance == Decimal("-5")
    assert (await fulfill(store, "send_email", 1, {"device": alice}, provider, allow_partial=True)).granted == 0


async def test_a_reversal_after_adjust_returns_to_the_kept_wallet(store, alice, provider):
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("10", SwitchPolicy.ADJUST)])
    result = await fulfill(store, "send_email", 8, {"device": alice}, provider)
    await plan_switch(store, {"user": alice}, [_spec("20", SwitchPolicy.ADJUST)])

    await unfulfill(store, result.guid)

    assert (await store.get_wallet(wallet.id)).balance == Decimal("20")


async def test_adjust_on_a_windowed_wallet_changes_the_allotment_and_keeps_the_window(store, alice):
    daily = {"time": "00:00:00"}
    [wallet] = await plan_switch(store, {"user": alice}, [_spec("200", SwitchPolicy.ADJUST, reset_config=daily)])

    await plan_switch(store, {"user": alice}, [_spec("500", SwitchPolicy.ADJUST, reset_config=daily)])

    stored = await store.get_wallet(wallet.id)
    assert stored.balance == Decimal("500")
    assert stored.valid_until is None
    assert [t for t in await transactions_for_wallet(store, wallet.id) if t.kind == TransactionKind.GRANT] == []


async def test_a_wallet_opened_under_replace_is_replaced_by_a_keep_spec(store, alice):
    old = await open_wallet(store, alice, balance="5")

    [new] = await plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])

    assert new.id != old.id
    assert (await store.get_wallet(old.id)).valid_until is not None
    assert new.balance == Decimal("100")


async def test_a_change_between_windowed_and_stored_balance_replaces_the_wallet(store, alice):
    [old] = await plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.ADJUST)])

    [new] = await plan_switch(
        store, {"user": alice}, [_spec("100", SwitchPolicy.ADJUST, reset_config={"time": "00:00:00"})]
    )

    assert new.id != old.id
    assert (await store.get_wallet(old.id)).valid_until is not None


async def test_a_wallet_absent_from_the_new_plan_is_closed(store, alice):
    [credits_wallet, storage] = await plan_switch(
        store,
        {"user": alice},
        [
            _spec("100", SwitchPolicy.TOP_UP),
            WalletSpec(WalletType.GRANT, "storage", Decimal("10"), on_switch=SwitchPolicy.ADJUST),
        ],
    )

    await plan_switch(store, {"user": alice}, [_spec("100", SwitchPolicy.TOP_UP)])

    assert (await store.get_wallet(credits_wallet.id)).valid_until is None
    assert (await store.get_wallet(storage.id)).valid_until is not None


async def test_a_subject_given_under_two_keys_is_switched_once(store, alice):
    result = await plan_switch(
        store, {"a": alice, "b": alice}, [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("5"))]
    )

    live = [w for w in await wallets_for_subject(store, alice) if w.valid_until is None]
    assert len(live) == 1
    assert [w.id for w in result] == [live[0].id]
