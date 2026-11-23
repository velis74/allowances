"""``transfer`` and its reversal."""

import uuid

from datetime import timedelta
from decimal import Decimal

import pytest

from allowances.errors import InsufficientBalanceError, UnsuitableWalletError, WalletNotFoundError
from allowances.types import ObjectRef, TransactionKind, WalletSpec, WalletType
from allowances.windows import utcnow
from tests.api import (
    archive_expired_wallets,
    consumed,
    credit,
    effective_balance,
    find_unattached_fulfillment_records,
    fulfill,
    open_wallet,
    transfer,
    unfulfill,
)
from tests.conftest import open_wallet as seed_wallet
from tests.helpers import transaction_count, transactions_for_wallet

TRIP = ObjectRef("tours.trip", 5)


async def test_transfer_moves_the_amount_under_one_record(store, alice, bob):
    source = await seed_wallet(store, alice, balance="100")
    target = await seed_wallet(store, bob, balance="0", wallet_type=WalletType.GRANT)

    result = await transfer(store, source.id, target.id, "10", actor=alice)

    assert (await store.get_wallet(source.id)).balance == Decimal("90")
    assert (await store.get_wallet(target.id)).balance == Decimal("10")
    assert (result.requested, result.granted, result.satisfied) == (Decimal("10"), Decimal("10"), True)
    assert [(t.wallet_id, t.kind, t.amount, t.actor) for t in result.transactions] == [
        (source.id, TransactionKind.TRANSFER, Decimal("-10"), alice),
        (target.id, TransactionKind.TRANSFER, Decimal("10"), alice),
    ]
    assert {t.record_guid for t in result.transactions} == {result.guid}


async def test_transfer_files_under_a_given_guid_and_attaches_both_legs(store, alice, bob):
    source = await seed_wallet(store, alice, balance="100")
    target = await seed_wallet(store, bob, balance="0")
    guid = uuid.uuid4()

    result = await transfer(store, source.id, target.id, "10", guid=guid, obj=TRIP)

    assert result.guid == guid
    assert [t.obj for t in result.transactions] == [TRIP, TRIP]
    assert [t.obj for t in await store.transactions_for_record(record_guid=guid)] == [TRIP, TRIP]


async def test_transfer_refuses_more_than_the_source_holds_and_writes_nothing(store, alice, bob):
    source = await seed_wallet(store, alice, balance="5", overdraft_limit=Decimal("100"))
    target = await seed_wallet(store, bob, balance="0")

    with pytest.raises(InsufficientBalanceError) as exc_info:
        await transfer(store, source.id, target.id, "6")

    assert exc_info.value.result.granted == Decimal("5")
    assert exc_info.value.result.requested == Decimal("6")
    assert (await store.get_wallet(source.id)).balance == Decimal("5")
    assert (await store.get_wallet(target.id)).balance == Decimal("0")
    assert await transaction_count(store) == 0


@pytest.mark.parametrize("amount", ["0", "-1"])
async def test_transfer_refuses_a_non_positive_amount(store, alice, bob, amount):
    source = await seed_wallet(store, alice, balance="5")
    target = await seed_wallet(store, bob, balance="0")

    with pytest.raises(ValueError):
        await transfer(store, source.id, target.id, amount)


async def test_transfer_refuses_different_currencies(store, alice, bob):
    source = await seed_wallet(store, alice, balance="5")
    target = await seed_wallet(store, bob, balance="0", currency="eur")

    with pytest.raises(UnsuitableWalletError):
        await transfer(store, source.id, target.id, "1")
    assert await transaction_count(store) == 0


async def test_transfer_refuses_the_same_wallet(store, alice):
    wallet = await seed_wallet(store, alice, balance="5")

    with pytest.raises(UnsuitableWalletError):
        await transfer(store, wallet.id, wallet.id, "1")


async def test_transfer_refuses_an_unknown_wallet(store, alice):
    wallet = await seed_wallet(store, alice, balance="5")

    with pytest.raises(WalletNotFoundError):
        await transfer(store, wallet.id, 987654, "1")


async def test_transfer_refuses_an_expired_target(store, alice, bob):
    source = await seed_wallet(store, alice, balance="5")
    target = await seed_wallet(store, bob, balance="0", valid_until=utcnow() - timedelta(seconds=1))

    with pytest.raises(UnsuitableWalletError):
        await transfer(store, source.id, target.id, "1")


async def test_a_windowed_source_keeps_its_allotment_and_counts_the_transfer_in_its_window(store, alice, bob):
    source = await seed_wallet(store, alice, balance="10", reset_config={"window_seconds": 3600})
    target = await seed_wallet(store, bob, balance="0")

    await transfer(store, source.id, target.id, "4")

    assert (await store.get_wallet(source.id)).balance == Decimal("10")
    assert await effective_balance(store, await store.get_wallet(source.id)) == Decimal("6")


async def test_unfulfill_reverses_both_legs(store, alice, bob):
    source = await seed_wallet(store, alice, balance="100")
    target = await seed_wallet(store, bob, balance="0")
    result = await transfer(store, source.id, target.id, "10")

    await unfulfill(store, result.guid, actor=bob)

    assert (await store.get_wallet(source.id)).balance == Decimal("100")
    assert (await store.get_wallet(target.id)).balance == Decimal("0")
    stornos = [
        t for t in await store.transactions_for_record(record_guid=result.guid) if t.kind == TransactionKind.STORNO
    ]
    assert sorted((t.amount, t.actor) for t in stornos) == [(Decimal("-10"), bob), (Decimal("10"), bob)]


async def test_unfulfill_of_a_transfer_whose_target_spent_it_raises_and_writes_nothing(store, alice, bob, provider):
    source = await seed_wallet(store, alice, balance="100")
    target = await seed_wallet(store, bob, balance="0", overdraft_limit=Decimal("0"))
    result = await transfer(store, source.id, target.id, "10")
    await fulfill(store, "send_email", 4, {"device": bob}, provider)
    before = await transaction_count(store)

    with pytest.raises(InsufficientBalanceError) as exc_info:
        await unfulfill(store, result.guid)

    assert exc_info.value.result.requested == Decimal("10")
    assert exc_info.value.result.granted == Decimal("6")
    assert await transaction_count(store) == before
    assert (await store.get_wallet(source.id)).balance == Decimal("90")
    assert (await store.get_wallet(target.id)).balance == Decimal("6")


async def test_unfulfill_of_a_transfer_whose_target_is_archived_raises_and_writes_nothing(store, alice, bob):
    now = utcnow()
    source = await seed_wallet(store, alice, balance="100")
    target = await seed_wallet(store, bob, balance="0", valid_until=now + timedelta(days=1))
    result = await transfer(store, source.id, target.id, "10", at=now)
    assert await archive_expired_wallets(store, at=now + timedelta(days=2)) == 1
    before = await transaction_count(store)

    with pytest.raises(InsufficientBalanceError) as exc_info:
        await unfulfill(store, result.guid, at=now + timedelta(days=2))

    assert exc_info.value.result.requested == Decimal("10")
    assert exc_info.value.result.granted == Decimal("0")
    assert await transaction_count(store) == before
    assert (await store.get_wallet(source.id)).balance == Decimal("90")


async def test_unfulfill_of_a_transfer_whose_source_is_archived_raises_and_writes_nothing(store, alice, bob, provider):
    source = await seed_wallet(store, alice, balance="20")
    target = await seed_wallet(store, bob, balance="0")
    result = await transfer(store, source.id, target.id, "10")
    await fulfill(store, "send_email", 10, {"device": alice}, provider)
    assert await store.get_wallet(source.id) is None
    before = await transaction_count(store)

    with pytest.raises(WalletNotFoundError):
        await unfulfill(store, result.guid)

    assert await transaction_count(store) == before
    assert (await store.get_wallet(target.id)).balance == Decimal("10")


async def test_unfulfill_gives_back_consumption_under_the_same_guid_before_taking_the_transfer_back(
    store, alice, bob, provider
):
    source = await seed_wallet(store, alice, balance="20")
    target = await seed_wallet(store, bob, balance="0", overdraft_limit=Decimal("0"))
    result = await transfer(store, source.id, target.id, "10")
    await fulfill(store, "send_email", 10, {"device": bob}, provider, guid=result.guid)
    assert (await store.get_wallet(target.id)).balance == Decimal("0")

    await unfulfill(store, result.guid)

    assert (await store.get_wallet(source.id)).balance == Decimal("20")
    assert (await store.get_wallet(target.id)).balance == Decimal("0")


async def test_a_transfer_and_its_reversal_are_not_consumption(store, alice, bob):
    source = await seed_wallet(store, alice, balance="100")
    target = await seed_wallet(store, bob, balance="0")
    result = await transfer(store, source.id, target.id, "10")
    await unfulfill(store, result.guid)

    since, until = utcnow() - timedelta(days=1), utcnow() + timedelta(days=1)
    assert await consumed(store, source.id, since, until) == Decimal("0")
    assert await consumed(store, target.id, since, until) == Decimal("0")


async def test_unattached_transfers_and_credits_are_not_reported(store, alice, bob):
    source = await seed_wallet(store, alice, balance="100")
    target = await seed_wallet(store, bob, balance="0")

    await transfer(store, source.id, target.id, "10")
    await credit(store, target.id, "5", guid=uuid.uuid4())

    assert await find_unattached_fulfillment_records(store) == []


async def test_opening_and_funding_a_wallet_in_one_unit_of_work(store, alice, bob):
    source = await seed_wallet(store, alice, balance="15")

    async with store.unit_of_work():
        opened = await open_wallet(store, bob, WalletSpec(WalletType.CONSUMABLE, "credits"), source="budget")
        await transfer(store, source.id, opened.id, "10")

    assert (await store.get_wallet(opened.id)).balance == Decimal("10")
    assert (await store.get_wallet(source.id)).balance == Decimal("5")


async def test_a_failed_funding_transfer_rolls_back_the_opened_wallet(store, alice, bob):
    source = await seed_wallet(store, alice, balance="5")
    opened_ids = []

    with pytest.raises(InsufficientBalanceError):
        async with store.unit_of_work():
            opened = await open_wallet(store, bob, WalletSpec(WalletType.CONSUMABLE, "credits"), source="budget")
            opened_ids.append(opened.id)
            await transfer(store, source.id, opened.id, "10")

    assert await store.get_wallet(opened_ids[0]) is None
    assert [t.kind for t in await transactions_for_wallet(store, source.id)] == []


async def test_a_refused_transfer_caught_inside_an_outer_block_leaves_nothing_behind(store, alice, bob):
    source = await seed_wallet(store, alice, balance="5")
    target = await seed_wallet(store, bob, balance="0")
    opened_ids = []

    async with store.unit_of_work():
        opened = await open_wallet(store, bob, WalletSpec(WalletType.CONSUMABLE, "credits"), source="budget")
        opened_ids.append(opened.id)
        with pytest.raises(InsufficientBalanceError) as exc_info:
            await transfer(store, source.id, target.id, "10")

    assert await store.get_record(exc_info.value.result.guid) is None
    assert await transaction_count(store) == 0
    assert await store.get_wallet(opened_ids[0]) is not None
