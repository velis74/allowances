"""``transfer`` and its reversal."""

import uuid

from datetime import timedelta
from decimal import Decimal

import pytest

from allowances.engine import (
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
from allowances.errors import InsufficientBalanceError, UnsuitableWalletError, WalletNotFoundError
from allowances.types import ObjectRef, TransactionKind, WalletSpec, WalletType
from allowances.windows import utcnow
from tests.conftest import open_wallet as seed_wallet
from tests.helpers import transaction_count, transactions_for_wallet

TRIP = ObjectRef("tours.trip", 5)


def test_transfer_moves_the_amount_under_one_record(store, alice, bob):
    source = seed_wallet(store, alice, balance="100")
    target = seed_wallet(store, bob, balance="0", wallet_type=WalletType.GRANT)

    result = transfer(store, source.id, target.id, "10", actor=alice)

    assert store.get_wallet(source.id).balance == Decimal("90")
    assert store.get_wallet(target.id).balance == Decimal("10")
    assert (result.requested, result.granted, result.satisfied) == (Decimal("10"), Decimal("10"), True)
    assert [(t.wallet_id, t.kind, t.amount, t.actor) for t in result.transactions] == [
        (source.id, TransactionKind.TRANSFER, Decimal("-10"), alice),
        (target.id, TransactionKind.TRANSFER, Decimal("10"), alice),
    ]
    assert {t.record_guid for t in result.transactions} == {result.guid}


def test_transfer_files_under_a_given_guid_and_attaches_both_legs(store, alice, bob):
    source = seed_wallet(store, alice, balance="100")
    target = seed_wallet(store, bob, balance="0")
    guid = uuid.uuid4()

    result = transfer(store, source.id, target.id, "10", guid=guid, obj=TRIP)

    assert result.guid == guid
    assert [t.obj for t in result.transactions] == [TRIP, TRIP]
    assert [t.obj for t in store.transactions_for_record(record_guid=guid)] == [TRIP, TRIP]


def test_transfer_refuses_more_than_the_source_holds_and_writes_nothing(store, alice, bob):
    source = seed_wallet(store, alice, balance="5", overdraft_limit=Decimal("100"))
    target = seed_wallet(store, bob, balance="0")

    with pytest.raises(InsufficientBalanceError) as exc_info:
        transfer(store, source.id, target.id, "6")

    assert exc_info.value.result.granted == Decimal("5")
    assert exc_info.value.result.requested == Decimal("6")
    assert store.get_wallet(source.id).balance == Decimal("5")
    assert store.get_wallet(target.id).balance == Decimal("0")
    assert transaction_count(store) == 0


@pytest.mark.parametrize("amount", ["0", "-1"])
def test_transfer_refuses_a_non_positive_amount(store, alice, bob, amount):
    source = seed_wallet(store, alice, balance="5")
    target = seed_wallet(store, bob, balance="0")

    with pytest.raises(ValueError):
        transfer(store, source.id, target.id, amount)


def test_transfer_refuses_different_currencies(store, alice, bob):
    source = seed_wallet(store, alice, balance="5")
    target = seed_wallet(store, bob, balance="0", currency="eur")

    with pytest.raises(UnsuitableWalletError):
        transfer(store, source.id, target.id, "1")
    assert transaction_count(store) == 0


def test_transfer_refuses_the_same_wallet(store, alice):
    wallet = seed_wallet(store, alice, balance="5")

    with pytest.raises(UnsuitableWalletError):
        transfer(store, wallet.id, wallet.id, "1")


def test_transfer_refuses_an_unknown_wallet(store, alice):
    wallet = seed_wallet(store, alice, balance="5")

    with pytest.raises(WalletNotFoundError):
        transfer(store, wallet.id, 987654, "1")


def test_transfer_refuses_an_expired_target(store, alice, bob):
    source = seed_wallet(store, alice, balance="5")
    target = seed_wallet(store, bob, balance="0", valid_until=utcnow() - timedelta(seconds=1))

    with pytest.raises(UnsuitableWalletError):
        transfer(store, source.id, target.id, "1")


def test_a_windowed_source_keeps_its_allotment_and_counts_the_transfer_in_its_window(store, alice, bob):
    source = seed_wallet(store, alice, balance="10", reset_config={"window_seconds": 3600})
    target = seed_wallet(store, bob, balance="0")

    transfer(store, source.id, target.id, "4")

    assert store.get_wallet(source.id).balance == Decimal("10")
    assert effective_balance(store, store.get_wallet(source.id)) == Decimal("6")


def test_unfulfill_reverses_both_legs(store, alice, bob):
    source = seed_wallet(store, alice, balance="100")
    target = seed_wallet(store, bob, balance="0")
    result = transfer(store, source.id, target.id, "10")

    unfulfill(store, result.guid, actor=bob)

    assert store.get_wallet(source.id).balance == Decimal("100")
    assert store.get_wallet(target.id).balance == Decimal("0")
    stornos = [t for t in store.transactions_for_record(record_guid=result.guid) if t.kind == TransactionKind.STORNO]
    assert sorted((t.amount, t.actor) for t in stornos) == [(Decimal("-10"), bob), (Decimal("10"), bob)]


def test_unfulfill_of_a_transfer_whose_target_spent_it_raises_and_writes_nothing(store, alice, bob, provider):
    source = seed_wallet(store, alice, balance="100")
    target = seed_wallet(store, bob, balance="0", overdraft_limit=Decimal("0"))
    result = transfer(store, source.id, target.id, "10")
    fulfill(store, "send_email", 4, {"device": bob}, provider)
    before = transaction_count(store)

    with pytest.raises(InsufficientBalanceError) as exc_info:
        unfulfill(store, result.guid)

    assert exc_info.value.result.requested == Decimal("10")
    assert exc_info.value.result.granted == Decimal("6")
    assert transaction_count(store) == before
    assert store.get_wallet(source.id).balance == Decimal("90")
    assert store.get_wallet(target.id).balance == Decimal("6")


def test_unfulfill_of_a_transfer_whose_target_is_archived_raises_and_writes_nothing(store, alice, bob):
    now = utcnow()
    source = seed_wallet(store, alice, balance="100")
    target = seed_wallet(store, bob, balance="0", valid_until=now + timedelta(days=1))
    result = transfer(store, source.id, target.id, "10", at=now)
    assert archive_expired_wallets(store, at=now + timedelta(days=2)) == 1
    before = transaction_count(store)

    with pytest.raises(InsufficientBalanceError) as exc_info:
        unfulfill(store, result.guid, at=now + timedelta(days=2))

    assert exc_info.value.result.requested == Decimal("10")
    assert exc_info.value.result.granted == Decimal("0")
    assert transaction_count(store) == before
    assert store.get_wallet(source.id).balance == Decimal("90")


def test_unfulfill_of_a_transfer_whose_source_is_archived_raises_and_writes_nothing(store, alice, bob, provider):
    source = seed_wallet(store, alice, balance="20")
    target = seed_wallet(store, bob, balance="0")
    result = transfer(store, source.id, target.id, "10")
    fulfill(store, "send_email", 10, {"device": alice}, provider)
    assert store.get_wallet(source.id) is None
    before = transaction_count(store)

    with pytest.raises(WalletNotFoundError):
        unfulfill(store, result.guid)

    assert transaction_count(store) == before
    assert store.get_wallet(target.id).balance == Decimal("10")


def test_unfulfill_gives_back_consumption_under_the_same_guid_before_taking_the_transfer_back(
    store, alice, bob, provider
):
    source = seed_wallet(store, alice, balance="20")
    target = seed_wallet(store, bob, balance="0", overdraft_limit=Decimal("0"))
    result = transfer(store, source.id, target.id, "10")
    fulfill(store, "send_email", 10, {"device": bob}, provider, guid=result.guid)
    assert store.get_wallet(target.id).balance == Decimal("0")

    unfulfill(store, result.guid)

    assert store.get_wallet(source.id).balance == Decimal("20")
    assert store.get_wallet(target.id).balance == Decimal("0")


def test_a_transfer_and_its_reversal_are_not_consumption(store, alice, bob):
    source = seed_wallet(store, alice, balance="100")
    target = seed_wallet(store, bob, balance="0")
    result = transfer(store, source.id, target.id, "10")
    unfulfill(store, result.guid)

    since, until = utcnow() - timedelta(days=1), utcnow() + timedelta(days=1)
    assert consumed(store, source.id, since, until) == Decimal("0")
    assert consumed(store, target.id, since, until) == Decimal("0")


def test_unattached_transfers_and_credits_are_not_reported(store, alice, bob):
    source = seed_wallet(store, alice, balance="100")
    target = seed_wallet(store, bob, balance="0")

    transfer(store, source.id, target.id, "10")
    credit(store, target.id, "5", guid=uuid.uuid4())

    assert find_unattached_fulfillment_records(store) == []


def test_opening_and_funding_a_wallet_in_one_unit_of_work(store, alice, bob):
    source = seed_wallet(store, alice, balance="15")

    with store.unit_of_work():
        opened = open_wallet(store, bob, WalletSpec(WalletType.CONSUMABLE, "credits"), source="budget")
        transfer(store, source.id, opened.id, "10")

    assert store.get_wallet(opened.id).balance == Decimal("10")
    assert store.get_wallet(source.id).balance == Decimal("5")


def test_a_failed_funding_transfer_rolls_back_the_opened_wallet(store, alice, bob):
    source = seed_wallet(store, alice, balance="5")
    opened_ids = []

    with pytest.raises(InsufficientBalanceError):
        with store.unit_of_work():
            opened = open_wallet(store, bob, WalletSpec(WalletType.CONSUMABLE, "credits"), source="budget")
            opened_ids.append(opened.id)
            transfer(store, source.id, opened.id, "10")

    assert store.get_wallet(opened_ids[0]) is None
    assert [t.kind for t in transactions_for_wallet(store, source.id)] == []


def test_a_refused_transfer_caught_inside_an_outer_block_leaves_nothing_behind(store, alice, bob):
    source = seed_wallet(store, alice, balance="5")
    target = seed_wallet(store, bob, balance="0")
    opened_ids = []

    with store.unit_of_work():
        opened = open_wallet(store, bob, WalletSpec(WalletType.CONSUMABLE, "credits"), source="budget")
        opened_ids.append(opened.id)
        with pytest.raises(InsufficientBalanceError) as exc_info:
            transfer(store, source.id, target.id, "10")

    assert store.get_record(exc_info.value.result.guid) is None
    assert transaction_count(store) == 0
    assert store.get_wallet(opened_ids[0]) is not None
