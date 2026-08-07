"""``attach`` and ``unfulfill`` — closing the loop after a fulfillment, and undoing one."""

import uuid

from decimal import Decimal

import pytest

from allowances.engine import attach, fulfill, unfulfill
from allowances.errors import RecordNotFoundError
from allowances.types import ObjectRef, TransactionKind
from tests.conftest import open_wallet
from tests.helpers import transactions_for_wallet

EMAIL = ObjectRef("mail.message", 1001)
OTHER_EMAIL = ObjectRef("mail.message", 2002)


def test_attach_marks_every_transaction_under_the_guid(store, alice, provider):
    open_wallet(store, alice, balance="10")
    result = fulfill(store, "send_email", 3, {"device": alice}, provider)

    attach(store, result.guid, EMAIL)

    attached = store.transactions_for_record(record_guid=result.guid)
    assert [t.obj for t in attached] == [EMAIL]


def test_attach_spans_several_fulfill_calls_sharing_one_guid(store, alice, provider):
    open_wallet(store, alice, balance="100")
    guid = uuid.uuid4()
    fulfill(store, "send_email", 2, {"device": alice}, provider, guid=guid)
    fulfill(store, "send_email", 3, {"device": alice}, provider, guid=guid)

    attach(store, guid, EMAIL)

    assert all(t.obj == EMAIL for t in store.transactions_for_record(record_guid=guid))


def test_attach_leaves_already_attached_transactions_alone(store, alice, provider):
    open_wallet(store, alice, balance="10")
    result = fulfill(store, "send_email", 3, {"device": alice}, provider)
    attach(store, result.guid, EMAIL)

    attach(store, result.guid, OTHER_EMAIL)

    assert [t.obj for t in store.transactions_for_record(record_guid=result.guid)] == [EMAIL]


def test_attach_on_an_unknown_guid_raises(store):
    with pytest.raises(RecordNotFoundError):
        attach(store, uuid.uuid4(), EMAIL)


def test_unfulfill_gives_the_balance_back(store, alice, provider):
    wallet = open_wallet(store, alice, balance="10")
    result = fulfill(store, "send_email", 3, {"device": alice}, provider)
    assert store.get_wallet(wallet.id).balance == Decimal("7")

    unfulfill(store, result.guid)

    assert store.get_wallet(wallet.id).balance == Decimal("10")
    storno = [t for t in transactions_for_wallet(store, wallet.id) if t.kind == TransactionKind.STORNO]
    assert len(storno) == 1
    assert storno[0].storno_of_id == result.transactions[0].id
    assert storno[0].amount == Decimal("3")


def test_unfulfill_keeps_the_original_entry_rather_than_deleting_it(store, alice, provider):
    wallet = open_wallet(store, alice, balance="10")
    result = fulfill(store, "send_email", 3, {"device": alice}, provider)

    unfulfill(store, result.guid)

    kinds = sorted(str(t.kind) for t in transactions_for_wallet(store, wallet.id))
    assert kinds == ["consumption", "storno"]


def test_unfulfill_is_idempotent(store, alice, provider):
    wallet = open_wallet(store, alice, balance="10")
    result = fulfill(store, "send_email", 3, {"device": alice}, provider)

    unfulfill(store, result.guid)
    unfulfill(store, result.guid)

    assert store.get_wallet(wallet.id).balance == Decimal("10")
    storno = [t for t in transactions_for_wallet(store, wallet.id) if t.kind == TransactionKind.STORNO]
    assert len(storno) == 1


def test_unfulfill_reverses_every_wallet_a_request_touched(store, alice, bob):
    from tests.conftest import CREDITS, provider_with

    first = open_wallet(store, alice, balance="2", overdraft_limit=Decimal("0"))
    second = open_wallet(store, bob, balance="5")
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
    result = fulfill(store, "send_email", 4, {"device": alice, "team": bob}, provider)

    unfulfill(store, result.guid)

    assert store.get_wallet(first.id).balance == Decimal("2")
    assert store.get_wallet(second.id).balance == Decimal("5")


def test_unfulfill_on_an_unknown_guid_raises(store):
    with pytest.raises(RecordNotFoundError):
        unfulfill(store, uuid.uuid4())


def test_unfulfill_skips_transactions_whose_wallet_was_archived(store, alice, provider):
    # Drained to exactly zero, so the wallet is archived on the spot and there is nothing left to
    # credit back. The call must still succeed rather than fail on the missing wallet.
    wallet = open_wallet(store, alice, balance="3")
    result = fulfill(store, "send_email", 3, {"device": alice}, provider)
    assert store.get_wallet(wallet.id) is None

    unfulfill(store, result.guid)

    assert store.transactions_for_record(record_guid=result.guid) == []
