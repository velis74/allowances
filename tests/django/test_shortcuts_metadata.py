"""The shortcuts pass application metadata through to the ledger entries, once per flavour (``dj``)."""

import uuid

from decimal import Decimal

import pytest

from django.contrib.auth.models import User
from django.test import override_settings

from allowances.types import TransactionKind, WalletSpec, WalletType
from allowances_django.models import Transaction, TransactionArchive

CONFIG = {
    "features": {
        "send_email": {
            "consumable": {"currencies": ["credits"]},
            "subjects": {"device"},
            "fulfillment_order": [("consumable", "device")],
        }
    },
    "currencies": {"credits": {"general": True, "round_fn": None}},
}
METADATA = {"campaign": "pomlad — čšž", "tags": ["a", 1, True, None], "nested": {"ok": False}}


@pytest.fixture
def alice():
    return User.objects.create(username="alice")


def _opened(dj, subject, balance):
    return dj.open_wallet(subject, WalletSpec(WalletType.CONSUMABLE, "credits", Decimal(balance)), source="budget")


@override_settings(ALLOWANCES=CONFIG)
def test_shortcut_fulfill_records_the_metadata(dj, alice):
    _opened(dj, alice, "10")

    result = dj.fulfill("send_email", 3, {"device": alice}, metadata=METADATA)

    assert [t.metadata for t in result.transactions] == [METADATA]
    assert [t.metadata for t in Transaction.objects.filter(record__guid=result.guid)] == [METADATA]


@override_settings(ALLOWANCES=CONFIG)
def test_shortcut_fulfill_without_metadata_records_null(dj, alice):
    _opened(dj, alice, "10")

    result = dj.fulfill("send_email", 3, {"device": alice})

    assert [t.metadata for t in Transaction.objects.filter(record__guid=result.guid)] == [None]


def test_shortcut_credit_records_the_metadata(dj, alice):
    wallet = _opened(dj, alice, "10")

    txn = dj.credit(wallet, "5", metadata={"invoice": "2026-0042"})

    assert txn.metadata == {"invoice": "2026-0042"}
    assert Transaction.objects.get(kind=TransactionKind.GRANT).metadata == {"invoice": "2026-0042"}


def test_shortcut_transfer_records_the_metadata_on_both_legs(dj, alice):
    guide = User.objects.create(username="guide")
    budget, pocket = _opened(dj, guide, "100"), _opened(dj, alice, "0")

    result = dj.transfer(budget, pocket, "10", metadata={"trip": "Bled"})

    legs = Transaction.objects.filter(record__guid=result.guid).order_by("id")
    assert [(t.wallet_id, t.amount, t.metadata) for t in legs] == [
        (budget.id, Decimal("-10"), {"trip": "Bled"}),
        (pocket.id, Decimal("10"), {"trip": "Bled"}),
    ]


@override_settings(ALLOWANCES=CONFIG)
def test_shortcut_unfulfill_copies_each_entry_s_metadata_to_its_storno(dj, alice):
    wallet = _opened(dj, alice, "100")
    guid = uuid.uuid4()
    dj.fulfill("send_email", 3, {"device": alice}, guid=guid, metadata={"step": 1})
    dj.fulfill("send_email", 4, {"device": alice}, guid=guid)
    dj.credit(wallet, "5", guid=guid, metadata={"refund": True})

    dj.unfulfill(guid)

    stornos = Transaction.objects.filter(kind=TransactionKind.STORNO).select_related("storno_of")
    assert stornos.count() == 3
    for storno in stornos:
        assert storno.metadata == storno.storno_of.metadata
    assert sorted((t.amount, t.metadata is None) for t in stornos) == [
        (Decimal("-5"), False),
        (Decimal("3"), False),
        (Decimal("4"), True),
    ]


@override_settings(ALLOWANCES=CONFIG)
def test_a_wallet_drained_through_the_shortcut_keeps_the_metadata_in_the_archive(dj, alice):
    wallet = _opened(dj, alice, "5")

    dj.fulfill("send_email", 5, {"device": alice}, metadata=METADATA)

    archived = TransactionArchive.objects.get(original_wallet_id=wallet.id)
    assert (archived.kind, archived.amount, archived.metadata) == (TransactionKind.CONSUMPTION, Decimal("-5"), METADATA)
