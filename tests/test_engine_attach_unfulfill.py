"""``attach`` and ``unfulfill``: linking a fulfilment to its object, and reversing one."""

import uuid

from decimal import Decimal

import pytest

from allowances.errors import RecordNotFoundError
from allowances.types import ObjectRef, TransactionKind
from tests.api import attach, fulfill, unfulfill
from tests.conftest import open_wallet
from tests.helpers import transactions_for_wallet

EMAIL = ObjectRef("mail.message", 1001)
OTHER_EMAIL = ObjectRef("mail.message", 2002)


async def test_attach_marks_every_transaction_under_the_guid(store, alice, provider):
    await open_wallet(store, alice, balance="10")
    result = await fulfill(store, "send_email", 3, {"device": alice}, provider)

    await attach(store, result.guid, EMAIL)

    attached = await store.transactions_for_record(record_guid=result.guid)
    assert [t.obj for t in attached] == [EMAIL]


async def test_attach_spans_several_fulfill_calls_sharing_one_guid(store, alice, provider):
    await open_wallet(store, alice, balance="100")
    guid = uuid.uuid4()
    await fulfill(store, "send_email", 2, {"device": alice}, provider, guid=guid)
    await fulfill(store, "send_email", 3, {"device": alice}, provider, guid=guid)

    await attach(store, guid, EMAIL)

    assert all(t.obj == EMAIL for t in await store.transactions_for_record(record_guid=guid))


async def test_attach_leaves_already_attached_transactions_alone(store, alice, provider):
    await open_wallet(store, alice, balance="10")
    result = await fulfill(store, "send_email", 3, {"device": alice}, provider)
    await attach(store, result.guid, EMAIL)

    await attach(store, result.guid, OTHER_EMAIL)

    assert [t.obj for t in await store.transactions_for_record(record_guid=result.guid)] == [EMAIL]


async def test_attach_on_an_unknown_guid_raises(store):
    with pytest.raises(RecordNotFoundError):
        await attach(store, uuid.uuid4(), EMAIL)


async def test_unfulfill_gives_the_balance_back(store, alice, provider):
    wallet = await open_wallet(store, alice, balance="10")
    result = await fulfill(store, "send_email", 3, {"device": alice}, provider)
    assert (await store.get_wallet(wallet.id)).balance == Decimal("7")

    await unfulfill(store, result.guid)

    assert (await store.get_wallet(wallet.id)).balance == Decimal("10")
    storno = [t for t in await transactions_for_wallet(store, wallet.id) if t.kind == TransactionKind.STORNO]
    assert len(storno) == 1
    assert storno[0].storno_of_id == result.transactions[0].id
    assert storno[0].amount == Decimal("3")


async def test_unfulfill_keeps_the_original_entry_rather_than_deleting_it(store, alice, provider):
    wallet = await open_wallet(store, alice, balance="10")
    result = await fulfill(store, "send_email", 3, {"device": alice}, provider)

    await unfulfill(store, result.guid)

    kinds = sorted(str(t.kind) for t in await transactions_for_wallet(store, wallet.id))
    assert kinds == ["consumption", "storno"]


async def test_unfulfill_is_idempotent(store, alice, provider):
    wallet = await open_wallet(store, alice, balance="10")
    result = await fulfill(store, "send_email", 3, {"device": alice}, provider)

    await unfulfill(store, result.guid)
    await unfulfill(store, result.guid)

    assert (await store.get_wallet(wallet.id)).balance == Decimal("10")
    storno = [t for t in await transactions_for_wallet(store, wallet.id) if t.kind == TransactionKind.STORNO]
    assert len(storno) == 1


async def test_unfulfill_reverses_every_wallet_a_request_touched(store, alice, bob):
    from tests.conftest import CREDITS, provider_with

    first = await open_wallet(store, alice, balance="2", overdraft_limit=Decimal("0"))
    second = await open_wallet(store, bob, balance="5")
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device", "team"},
                "fulfillment_order": [("consumable", "device"), ("consumable", "team")],
            }
        },
        currencies=CREDITS,
    )
    result = await fulfill(store, "send_email", 4, {"device": alice, "team": bob}, provider)

    await unfulfill(store, result.guid)

    assert (await store.get_wallet(first.id)).balance == Decimal("2")
    assert (await store.get_wallet(second.id)).balance == Decimal("5")


async def test_unfulfill_on_an_unknown_guid_raises(store):
    with pytest.raises(RecordNotFoundError):
        await unfulfill(store, uuid.uuid4())


async def test_unfulfill_skips_transactions_whose_wallet_was_archived(store, alice, provider):
    # The wallet drains to zero and is archived; unfulfill succeeds without a live wallet.
    wallet = await open_wallet(store, alice, balance="3")
    result = await fulfill(store, "send_email", 3, {"device": alice}, provider)
    assert await store.get_wallet(wallet.id) is None

    await unfulfill(store, result.guid)

    assert await store.transactions_for_record(record_guid=result.guid) == []
