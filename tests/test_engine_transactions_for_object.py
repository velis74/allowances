"""``transactions_for_object``: every ledger entry an object caused, with the stornos that reversed them."""

import uuid

from datetime import timedelta
from decimal import Decimal

import pytest

import allowances
import allowances.sync

from allowances.backends import MemoryStore
from allowances.errors import UnsupportedOperationError
from allowances.types import ObjectRef, TransactionKind
from allowances.windows import utcnow
from tests.api import archive_expired_wallets, attach, credit, fulfill, transactions_for_object, transfer, unfulfill
from tests.conftest import CREDITS, open_wallet, provider_with

EMAIL = ObjectRef("mail.message", 1001)
OTHER_EMAIL = ObjectRef("mail.message", 2002)
TRIP = ObjectRef("tours.trip", 5)


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


def _net(entries) -> Decimal:
    return sum((t.amount for t in entries), Decimal("0"))


async def test_an_attached_fulfilment_is_the_object_s_entry(store, alice, provider):
    wallet = await open_wallet(store, alice, balance="10")
    result = await fulfill(store, "send_email", 3, {"device": alice}, provider, metadata={"campaign": "spring"})
    await attach(store, result.guid, EMAIL)

    entries = await transactions_for_object(store, EMAIL)

    assert [(t.wallet_id, t.kind, t.amount, t.obj, t.actor, t.metadata) for t in entries] == [
        (wallet.id, TransactionKind.CONSUMPTION, Decimal("-3"), EMAIL, alice, {"campaign": "spring"})
    ]
    assert [t.record_guid for t in entries] == [result.guid]
    assert [t.id for t in entries] == [t.id for t in result.transactions]


async def test_an_unattached_fulfilment_belongs_to_no_object(store, alice, provider):
    await open_wallet(store, alice, balance="10")
    await fulfill(store, "send_email", 3, {"device": alice}, provider)

    assert await transactions_for_object(store, EMAIL) == []


async def test_an_unknown_object_has_no_entries(store):
    assert await transactions_for_object(store, ObjectRef("mail.message", 987654)) == []


async def test_unfulfill_after_attach_adds_the_stornos_and_nets_to_zero(store, alice, bob, provider):
    wallet = await open_wallet(store, alice, balance="10")
    result = await fulfill(store, "send_email", 3, {"device": alice}, provider, metadata={"step": 1})
    await attach(store, result.guid, EMAIL)

    await unfulfill(store, result.guid, actor=bob)

    entries = await transactions_for_object(store, EMAIL)
    assert [(t.wallet_id, t.kind, t.amount) for t in entries] == [
        (wallet.id, TransactionKind.CONSUMPTION, Decimal("-3")),
        (wallet.id, TransactionKind.STORNO, Decimal("3")),
    ]
    original, storno = entries
    assert storno.storno_of_id == original.id
    assert (storno.actor, storno.metadata) == (bob, {"step": 1})
    assert _net(entries) == Decimal("0")


async def test_a_repeated_unfulfill_adds_no_second_storno(store, alice, provider):
    await open_wallet(store, alice, balance="10")
    result = await fulfill(store, "send_email", 3, {"device": alice}, provider)
    await attach(store, result.guid, EMAIL)

    await unfulfill(store, result.guid)
    await unfulfill(store, result.guid)

    assert [t.kind for t in await transactions_for_object(store, EMAIL)] == [
        TransactionKind.CONSUMPTION,
        TransactionKind.STORNO,
    ]


async def test_a_fulfilment_drawn_from_several_wallets_returns_every_entry(store, alice, bob):
    own = await open_wallet(store, alice, balance="2", reset_config={"window_seconds": 3600})
    team = await open_wallet(store, bob, balance="10")
    result = await fulfill(store, "send_email", 5, {"device": alice, "team": bob}, _two_wallet_provider())
    await attach(store, result.guid, EMAIL)

    entries = await transactions_for_object(store, EMAIL)

    assert sorted((t.wallet_id, t.amount) for t in entries) == sorted(
        [(own.id, Decimal("-2")), (team.id, Decimal("-3"))]
    )
    assert {t.obj for t in entries} == {EMAIL}
    assert _net(entries) == Decimal("-5")

    await unfulfill(store, result.guid)

    entries = await transactions_for_object(store, EMAIL)
    assert sorted(str(t.kind) for t in entries) == ["consumption", "consumption", "storno", "storno"]
    assert _net(entries) == Decimal("0")


async def test_a_credit_attached_to_an_object_is_its_entry(store, alice, bob):
    wallet = await open_wallet(store, alice, balance="10")

    txn = await credit(store, wallet.id, "5", obj=TRIP, actor=bob, metadata={"invoice": "2026-0042"})

    entries = await transactions_for_object(store, TRIP)
    assert [(t.id, t.kind, t.amount, t.obj, t.actor, t.metadata) for t in entries] == [
        (txn.id, TransactionKind.GRANT, Decimal("5"), TRIP, bob, {"invoice": "2026-0042"})
    ]


async def test_a_reversed_credit_returns_its_storno(store, alice):
    wallet = await open_wallet(store, alice, balance="10")
    guid = uuid.uuid4()
    await credit(store, wallet.id, "5", guid=guid, obj=TRIP)

    await unfulfill(store, guid)

    entries = await transactions_for_object(store, TRIP)
    assert [(t.kind, t.amount) for t in entries] == [
        (TransactionKind.GRANT, Decimal("5")),
        (TransactionKind.STORNO, Decimal("-5")),
    ]
    assert _net(entries) == Decimal("0")


async def test_both_legs_of_a_transfer_attached_to_an_object_are_its_entries(store, alice, bob):
    source = await open_wallet(store, alice, balance="100")
    target = await open_wallet(store, bob, balance="0")

    result = await transfer(store, source.id, target.id, "10", obj=TRIP, actor=alice, metadata={"trip": "Bled"})

    entries = await transactions_for_object(store, TRIP)
    assert [(t.wallet_id, t.kind, t.amount, t.actor, t.metadata) for t in entries] == [
        (source.id, TransactionKind.TRANSFER, Decimal("-10"), alice, {"trip": "Bled"}),
        (target.id, TransactionKind.TRANSFER, Decimal("10"), alice, {"trip": "Bled"}),
    ]
    assert {t.record_guid for t in entries} == {result.guid}


async def test_a_reversed_transfer_returns_four_entries_netting_to_zero(store, alice, bob):
    source = await open_wallet(store, alice, balance="100")
    target = await open_wallet(store, bob, balance="0")
    result = await transfer(store, source.id, target.id, "10", obj=TRIP)

    await unfulfill(store, result.guid)

    entries = await transactions_for_object(store, TRIP)
    assert [t.kind for t in entries] == [
        TransactionKind.TRANSFER,
        TransactionKind.TRANSFER,
        TransactionKind.STORNO,
        TransactionKind.STORNO,
    ]
    by_id = {t.id: t for t in entries}
    for storno in entries[2:]:
        assert storno.amount == -by_id[storno.storno_of_id].amount
    assert _net(entries) == Decimal("0")


async def test_another_object_s_entries_are_not_returned(store, alice, provider):
    await open_wallet(store, alice, balance="100")
    first = await fulfill(store, "send_email", 3, {"device": alice}, provider)
    await attach(store, first.guid, EMAIL)
    second = await fulfill(store, "send_email", 4, {"device": alice}, provider)
    await attach(store, second.guid, OTHER_EMAIL)

    assert [t.amount for t in await transactions_for_object(store, EMAIL)] == [Decimal("-3")]
    assert [t.amount for t in await transactions_for_object(store, OTHER_EMAIL)] == [Decimal("-4")]


async def test_objects_sharing_one_record_each_get_only_their_own_entries_and_stornos(store, alice, provider):
    await open_wallet(store, alice, balance="100")
    guid = uuid.uuid4()
    await fulfill(store, "send_email", 3, {"device": alice}, provider, guid=guid)
    await attach(store, guid, EMAIL)
    await fulfill(store, "send_email", 4, {"device": alice}, provider, guid=guid)
    await attach(store, guid, OTHER_EMAIL)

    await unfulfill(store, guid)

    email = await transactions_for_object(store, EMAIL)
    other = await transactions_for_object(store, OTHER_EMAIL)
    assert [(t.kind, t.amount) for t in email] == [
        (TransactionKind.CONSUMPTION, Decimal("-3")),
        (TransactionKind.STORNO, Decimal("3")),
    ]
    assert [(t.kind, t.amount) for t in other] == [
        (TransactionKind.CONSUMPTION, Decimal("-4")),
        (TransactionKind.STORNO, Decimal("4")),
    ]
    assert {t.id for t in email}.isdisjoint({t.id for t in other})


async def test_a_storno_of_an_unattached_entry_in_the_same_record_is_not_returned(store, alice, provider):
    await open_wallet(store, alice, balance="100")
    guid = uuid.uuid4()
    await fulfill(store, "send_email", 3, {"device": alice}, provider, guid=guid)
    await attach(store, guid, EMAIL)
    await fulfill(store, "send_email", 4, {"device": alice}, provider, guid=guid)

    await unfulfill(store, guid)

    assert [(t.kind, t.amount) for t in await transactions_for_object(store, EMAIL)] == [
        (TransactionKind.CONSUMPTION, Decimal("-3")),
        (TransactionKind.STORNO, Decimal("3")),
    ]


async def test_entries_on_an_archived_wallet_are_not_returned(store, alice, bob, provider):
    now = utcnow()
    expiring = await open_wallet(store, alice, balance="10", valid_until=now + timedelta(days=1))
    lasting = await open_wallet(store, bob, balance="10")
    result = await fulfill(store, "send_email", 3, {"device": alice}, provider, at=now)
    await attach(store, result.guid, EMAIL)
    await credit(store, lasting.id, "5", obj=EMAIL, at=now)
    assert len(await transactions_for_object(store, EMAIL)) == 2

    assert await archive_expired_wallets(store, at=now + timedelta(days=2)) == 1

    entries = await transactions_for_object(store, EMAIL)
    assert [(t.wallet_id, t.kind, t.amount) for t in entries] == [(lasting.id, TransactionKind.GRANT, Decimal("5"))]
    assert expiring.id not in {t.wallet_id for t in entries}


async def test_a_wallet_drained_and_archived_takes_its_entries_with_it(store, alice, provider):
    await open_wallet(store, alice, balance="3")
    result = await fulfill(store, "send_email", 3, {"device": alice}, provider)
    await attach(store, result.guid, EMAIL)

    assert await transactions_for_object(store, EMAIL) == []


async def test_entries_come_in_the_order_they_were_written(store, alice, bob, provider):
    now = utcnow()
    own = await open_wallet(store, alice, balance="100")
    other = await open_wallet(store, bob, balance="0")
    await credit(store, own.id, "5", obj=TRIP, at=now)
    spent = await fulfill(store, "send_email", 3, {"device": alice}, provider, at=now + timedelta(seconds=1))
    await attach(store, spent.guid, TRIP)
    moved = await transfer(store, own.id, other.id, "10", obj=TRIP, at=now + timedelta(seconds=2))
    await unfulfill(store, spent.guid, at=now + timedelta(seconds=3))

    entries = await transactions_for_object(store, TRIP)

    assert [(t.kind, t.amount) for t in entries] == [
        (TransactionKind.GRANT, Decimal("5")),
        (TransactionKind.CONSUMPTION, Decimal("-3")),
        (TransactionKind.TRANSFER, Decimal("-10")),
        (TransactionKind.TRANSFER, Decimal("10")),
        (TransactionKind.STORNO, Decimal("3")),
    ]
    assert [t.created_at for t in entries] == sorted(t.created_at for t in entries)
    assert entries[2].record_guid == moved.guid
    assert _net(entries) == Decimal("5")


async def test_a_storno_and_a_later_entry_with_the_same_timestamp_come_in_id_order(store, alice):
    now = utcnow()
    wallet = await open_wallet(store, alice, balance="10")
    guid = uuid.uuid4()
    granted = await credit(store, wallet.id, "5", guid=guid, obj=TRIP, at=now)
    await unfulfill(store, guid, at=now)
    later = await credit(store, wallet.id, "7", obj=TRIP, at=now)

    entries = await transactions_for_object(store, TRIP)

    assert [(t.kind, t.amount) for t in entries] == [
        (TransactionKind.GRANT, Decimal("5")),
        (TransactionKind.STORNO, Decimal("-5")),
        (TransactionKind.GRANT, Decimal("7")),
    ]
    assert entries[0].id == granted.id
    assert entries[2].id == later.id
    assert [t.id for t in entries] == sorted(t.id for t in entries)


class _StoreWithoutObjectLookup:
    """A store that implements every method of the port but the optional ``transactions_for_object``."""

    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        if name == "transactions_for_object":
            raise AttributeError(name)
        return getattr(self._store, name)


async def test_a_store_without_the_lookup_raises_unsupported_operation(alice, provider):
    store = _StoreWithoutObjectLookup(MemoryStore())
    await open_wallet(store, alice, balance="10")
    result = await allowances.fulfill(store, "send_email", 3, {"device": alice}, provider)
    await allowances.attach(store, result.guid, EMAIL)

    with pytest.raises(UnsupportedOperationError):
        await allowances.transactions_for_object(store, EMAIL)


def test_a_store_without_the_lookup_raises_unsupported_operation_through_the_sync_api(alice):
    store = _StoreWithoutObjectLookup(MemoryStore())

    with pytest.raises(UnsupportedOperationError):
        allowances.sync.transactions_for_object(store, EMAIL)
