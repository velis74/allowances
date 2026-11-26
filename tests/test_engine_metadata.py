"""Application metadata on ledger entries: recorded as given, read back unchanged, copied to stornos and archives."""

import uuid

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from allowances.backends import MemoryStore
from allowances.types import TransactionKind, WalletType
from allowances.windows import utcnow
from tests.api import archive_expired_wallets, credit, fulfill, transfer, unfulfill
from tests.conftest import CREDITS, open_wallet, provider_with
from tests.helpers import archived_wallet_ids

RIGHT = "edit_right"
NESTED = {
    "reason": "Monthly newsletter — žabe in čebele",
    "tags": ["news", "bulk", 3, 1.5, True, None],
    "request": {"ip": "192.0.2.1", "retry": False, "parent": None, "steps": [{"n": 1}, {"n": 2}]},
    "empty": {},
    "none": None,
}


async def _record(store, guid):
    return await store.transactions_for_record(record_guid=guid)


async def _archived(store, wallet_id) -> list:
    """``(kind, amount, metadata)`` of every archived entry of a wallet, sorted by amount."""
    if isinstance(store, MemoryStore):
        rows = [(t.kind, t.amount, t.metadata) for t in store.archived_transactions if t.wallet_id == wallet_id]
    else:
        from allowances_django.models import TransactionArchive

        query = TransactionArchive.objects.filter(original_wallet_id=wallet_id).values_list(
            "kind", "amount", "metadata"
        )
        rows = [(TransactionKind(kind), amount, metadata) async for kind, amount, metadata in query]
    return sorted(rows, key=lambda row: row[1])


def _two_wallet_provider():
    return provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device", "team"},
                "fulfillment_order": [("consumable", "device"), ("consumable", "team")],
            }
        },
        currencies=CREDITS,
    )


# -- fulfill ----------------------------------------------------------------------------------------


async def test_fulfill_records_the_metadata_on_its_consumption_entry(store, alice, provider):
    await open_wallet(store, alice, balance="10")

    result = await fulfill(store, "send_email", 3, {"device": alice}, provider, metadata={"campaign": "spring"})

    assert [t.metadata for t in result.transactions] == [{"campaign": "spring"}]
    assert [(t.kind, t.metadata) for t in await _record(store, result.guid)] == [
        (TransactionKind.CONSUMPTION, {"campaign": "spring"})
    ]


async def test_fulfill_without_metadata_records_none(store, alice, provider):
    await open_wallet(store, alice, balance="10")

    result = await fulfill(store, "send_email", 3, {"device": alice}, provider)

    assert [t.metadata for t in await _record(store, result.guid)] == [None]


async def test_fulfill_records_the_metadata_on_every_wallet_it_draws_from(store, alice, bob):
    first = await open_wallet(store, alice, balance="2", overdraft_limit=Decimal("0"))
    second = await open_wallet(store, bob, balance="5")

    result = await fulfill(
        store, "send_email", 4, {"device": alice, "team": bob}, _two_wallet_provider(), metadata={"batch": 7}
    )

    assert [(t.wallet_id, t.amount, t.metadata) for t in await _record(store, result.guid)] == [
        (first.id, Decimal("-2"), {"batch": 7}),
        (second.id, Decimal("-2"), {"batch": 7}),
    ]


async def test_fulfill_records_the_metadata_on_the_use_of_a_right(store, alice):
    provider = provider_with(
        features={
            "edit": {
                "grant": {"currencies": [(RIGHT, 0)]},
                "consumable": {"currencies": ["credits"]},
                "subjects": {"user"},
                "fulfillment_order": [("consumable", "user"), ("grant", "user")],
            }
        },
        currencies={**CREDITS, RIGHT: {}},
    )
    credits = await open_wallet(store, alice, balance="2", overdraft_limit=Decimal("0"))
    right = await open_wallet(store, alice, currency=RIGHT, balance="1", wallet_type=WalletType.GRANT)

    result = await fulfill(store, "edit", 5, {"user": alice}, provider, metadata={"document": "d-1"})

    assert [(t.wallet_id, t.amount, t.metadata) for t in await _record(store, result.guid)] == [
        (credits.id, Decimal("-2"), {"document": "d-1"}),
        (right.id, Decimal("0"), {"document": "d-1"}),
    ]


async def test_fulfill_calls_under_one_guid_keep_their_own_metadata(store, alice, provider):
    await open_wallet(store, alice, balance="10")
    guid = uuid.uuid4()

    await fulfill(store, "send_email", 2, {"device": alice}, provider, guid=guid, metadata={"step": 1})
    await fulfill(store, "send_email", 3, {"device": alice}, provider, guid=guid)

    assert sorted(((t.amount, t.metadata) for t in await _record(store, guid)), key=lambda row: row[0]) == [
        (Decimal("-3"), None),
        (Decimal("-2"), {"step": 1}),
    ]


# -- credit and transfer ----------------------------------------------------------------------------


async def test_credit_records_the_metadata_on_its_grant_entry(store, alice):
    wallet = await open_wallet(store, alice, balance="10")
    guid = uuid.uuid4()

    txn = await credit(store, wallet.id, "5", guid=guid, metadata={"invoice": "2026-0042"})

    assert txn.metadata == {"invoice": "2026-0042"}
    assert [(t.kind, t.metadata) for t in await _record(store, guid)] == [
        (TransactionKind.GRANT, {"invoice": "2026-0042"})
    ]


async def test_credit_without_metadata_records_none(store, alice):
    wallet = await open_wallet(store, alice, balance="10")
    guid = uuid.uuid4()

    txn = await credit(store, wallet.id, "5", guid=guid)

    assert txn.metadata is None
    assert [t.metadata for t in await _record(store, guid)] == [None]


async def test_transfer_records_the_metadata_on_both_legs(store, alice, bob):
    source = await open_wallet(store, alice, balance="100")
    target = await open_wallet(store, bob, balance="0")

    result = await transfer(store, source.id, target.id, "10", metadata={"trip": "Bled"})

    assert [t.metadata for t in result.transactions] == [{"trip": "Bled"}, {"trip": "Bled"}]
    assert [(t.wallet_id, t.amount, t.metadata) for t in await _record(store, result.guid)] == [
        (source.id, Decimal("-10"), {"trip": "Bled"}),
        (target.id, Decimal("10"), {"trip": "Bled"}),
    ]


async def test_transfer_without_metadata_records_none(store, alice, bob):
    source = await open_wallet(store, alice, balance="100")
    target = await open_wallet(store, bob, balance="0")

    result = await transfer(store, source.id, target.id, "10")

    assert [t.metadata for t in await _record(store, result.guid)] == [None, None]


# -- unfulfill --------------------------------------------------------------------------------------


async def test_a_storno_carries_the_metadata_of_the_entry_it_reverses(store, alice, provider):
    await open_wallet(store, alice, balance="10")
    result = await fulfill(store, "send_email", 3, {"device": alice}, provider, metadata={"campaign": "spring"})

    await unfulfill(store, result.guid)

    stornos = [t for t in await _record(store, result.guid) if t.kind == TransactionKind.STORNO]
    assert [(t.storno_of_id, t.amount, t.metadata) for t in stornos] == [
        (result.transactions[0].id, Decimal("3"), {"campaign": "spring"})
    ]


async def test_stornos_of_a_mixed_record_copy_each_entry_s_own_metadata(store, alice, bob, provider):
    wallet = await open_wallet(store, alice, balance="100")
    other = await open_wallet(store, bob, balance="0")
    guid = uuid.uuid4()
    await fulfill(store, "send_email", 2, {"device": alice}, provider, guid=guid, metadata={"step": 1})
    await fulfill(store, "send_email", 3, {"device": alice}, provider, guid=guid, metadata={"step": 2})
    await fulfill(store, "send_email", 4, {"device": alice}, provider, guid=guid)
    await credit(store, wallet.id, "5", guid=guid, metadata={"refund": True})
    await transfer(store, wallet.id, other.id, "6", guid=guid, metadata={"leg": "both"})

    await unfulfill(store, guid)

    entries = await _record(store, guid)
    originals = {t.id: t for t in entries if t.kind != TransactionKind.STORNO}
    stornos = [t for t in entries if t.kind == TransactionKind.STORNO]
    assert len(originals) == 6
    assert sorted(t.storno_of_id for t in stornos) == sorted(originals)
    for storno in stornos:
        original = originals[storno.storno_of_id]
        assert storno.amount == -original.amount
        assert storno.metadata == original.metadata
    assert sorted((str(t.metadata) for t in stornos)) == sorted(
        str(m) for m in [{"step": 1}, {"step": 2}, None, {"refund": True}, {"leg": "both"}, {"leg": "both"}]
    )


async def test_stornos_of_a_transfer_carry_the_metadata_of_each_leg(store, alice, bob):
    source = await open_wallet(store, alice, balance="100")
    target = await open_wallet(store, bob, balance="0")
    result = await transfer(store, source.id, target.id, "10", metadata={"trip": "Bled"})

    await unfulfill(store, result.guid)

    stornos = [t for t in await _record(store, result.guid) if t.kind == TransactionKind.STORNO]
    assert sorted((t.amount, t.metadata["trip"]) for t in stornos) == [
        (Decimal("-10"), "Bled"),
        (Decimal("10"), "Bled"),
    ]


# -- values -----------------------------------------------------------------------------------------


async def test_changing_the_caller_s_dict_afterwards_does_not_change_the_entry(store, alice, bob, provider):
    wallet = await open_wallet(store, alice, balance="100")
    other = await open_wallet(store, bob, balance="0")
    fulfilled_with = {"campaign": "spring", "tags": ["a"]}
    credited_with = {"invoice": "1", "lines": [{"n": 1}]}
    transferred_with = {"trip": "Bled", "nested": {"k": "v"}}

    fulfilled = await fulfill(store, "send_email", 3, {"device": alice}, provider, metadata=fulfilled_with)
    guid = uuid.uuid4()
    await credit(store, wallet.id, "5", guid=guid, metadata=credited_with)
    transferred = await transfer(store, wallet.id, other.id, "10", metadata=transferred_with)
    fulfilled_with["campaign"] = "autumn"
    fulfilled_with["tags"].append("b")
    credited_with["lines"][0]["n"] = 2
    credited_with["extra"] = True
    transferred_with["nested"]["k"] = "changed"

    assert [t.metadata for t in await _record(store, fulfilled.guid)] == [{"campaign": "spring", "tags": ["a"]}]
    assert [t.metadata for t in await _record(store, guid)] == [{"invoice": "1", "lines": [{"n": 1}]}]
    assert [t.metadata for t in await _record(store, transferred.guid)] == [
        {"trip": "Bled", "nested": {"k": "v"}},
        {"trip": "Bled", "nested": {"k": "v"}},
    ]


async def test_nested_json_values_round_trip(store, alice, bob, provider):
    wallet = await open_wallet(store, alice, balance="100")
    other = await open_wallet(store, bob, balance="0")

    fulfilled = await fulfill(store, "send_email", 3, {"device": alice}, provider, metadata=NESTED)
    guid = uuid.uuid4()
    await credit(store, wallet.id, "5", guid=guid, metadata=NESTED)
    transferred = await transfer(store, wallet.id, other.id, "10", metadata=NESTED)

    assert [t.metadata for t in await _record(store, fulfilled.guid)] == [NESTED]
    assert [t.metadata for t in await _record(store, guid)] == [NESTED]
    assert [t.metadata for t in await _record(store, transferred.guid)] == [NESTED, NESTED]


async def test_an_empty_dict_is_recorded_as_given(store, alice, provider):
    await open_wallet(store, alice, balance="10")

    result = await fulfill(store, "send_email", 3, {"device": alice}, provider, metadata={})

    assert [t.metadata for t in await _record(store, result.guid)] == [{}]


# -- archive ----------------------------------------------------------------------------------------


async def test_a_drained_wallet_keeps_the_metadata_in_the_archive(store, alice, provider):
    wallet = await open_wallet(store, alice, balance="5")

    await fulfill(store, "send_email", 5, {"device": alice}, provider, metadata=NESTED)

    assert await archived_wallet_ids(store) == [wallet.id]
    assert await _archived(store, wallet.id) == [(TransactionKind.CONSUMPTION, Decimal("-5"), NESTED)]


async def test_an_expired_wallet_keeps_every_entry_s_metadata_in_the_archive(store, alice, bob, provider):
    now = utcnow()
    wallet = await open_wallet(store, alice, balance="10")
    other = await open_wallet(store, bob, balance="0")
    await credit(store, wallet.id, "2", metadata={"invoice": "7"})
    fulfilled = await fulfill(store, "send_email", 3, {"device": alice}, provider, metadata={"campaign": "spring"})
    await unfulfill(store, fulfilled.guid)
    await fulfill(store, "send_email", 1, {"device": alice}, provider)
    await transfer(store, wallet.id, other.id, "4", metadata={"trip": "Bled"})

    await store.save_wallet(replace(await store.get_wallet(wallet.id), valid_until=now - timedelta(seconds=1)))
    assert await archive_expired_wallets(store, at=now) == 1

    assert await _archived(store, wallet.id) == [
        (TransactionKind.TRANSFER, Decimal("-4"), {"trip": "Bled"}),
        (TransactionKind.CONSUMPTION, Decimal("-3"), {"campaign": "spring"}),
        (TransactionKind.CONSUMPTION, Decimal("-1"), None),
        (TransactionKind.GRANT, Decimal("2"), {"invoice": "7"}),
        (TransactionKind.STORNO, Decimal("3"), {"campaign": "spring"}),
    ]


@pytest.mark.parametrize("metadata", [None, {"k": "v"}])
async def test_archive_expired_wallets_carries_none_and_dicts_alike(store, alice, metadata):
    now = utcnow()
    wallet = await open_wallet(store, alice, balance="0")
    await credit(store, wallet.id, "3", metadata=metadata)

    await store.save_wallet(replace(await store.get_wallet(wallet.id), valid_until=now - timedelta(seconds=1)))
    await archive_expired_wallets(store, at=now)

    assert await _archived(store, wallet.id) == [(TransactionKind.GRANT, Decimal("3"), metadata)]
