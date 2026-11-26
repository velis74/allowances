"""The ``transactions_for_object`` shortcut takes a model instance as the object, once per flavour (``dj``)."""

from decimal import Decimal

import pytest

from django.contrib.auth.models import User
from django.test import override_settings

from allowances.types import ObjectRef, TransactionKind, WalletSpec, WalletType
from allowances_django.subjects import object_ref, subject_ref

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


@pytest.fixture
def alice():
    return User.objects.create(username="alice")


@pytest.fixture
def message():
    """Any saved model instance serves as the object a fulfilment paid for."""
    return User.objects.create(username="message")


def _opened(dj, subject, balance):
    return dj.open_wallet(subject, WalletSpec(WalletType.CONSUMABLE, "credits", Decimal(balance)), source="budget")


@override_settings(ALLOWANCES=CONFIG)
def test_shortcut_returns_the_entries_attached_to_a_model_instance(dj, alice, message):
    wallet = _opened(dj, alice, "10")
    result = dj.fulfill("send_email", 3, {"device": alice}, metadata={"campaign": "spring"})
    dj.attach(result.guid, message)

    entries = dj.transactions_for_object(message)

    assert [(t.wallet_id, t.kind, t.amount, t.obj, t.metadata) for t in entries] == [
        (wallet.id, TransactionKind.CONSUMPTION, Decimal("-3"), object_ref(message), {"campaign": "spring"})
    ]
    assert entries[0].obj == ObjectRef("auth.user", str(message.pk))


@override_settings(ALLOWANCES=CONFIG)
def test_shortcut_accepts_an_object_ref_as_well(dj, alice, message):
    _opened(dj, alice, "10")
    result = dj.fulfill("send_email", 3, {"device": alice})
    dj.attach(result.guid, message)

    by_instance = dj.transactions_for_object(message)
    by_ref = dj.transactions_for_object(ObjectRef("auth.user", message.pk))

    assert [t.id for t in by_ref] == [t.id for t in by_instance]
    assert len(by_ref) == 1


@override_settings(ALLOWANCES=CONFIG)
def test_shortcut_returns_the_stornos_of_a_reversed_fulfilment(dj, alice, message):
    _opened(dj, alice, "10")
    result = dj.fulfill("send_email", 3, {"device": alice})
    dj.attach(result.guid, message)

    dj.unfulfill(result.guid, actor=alice)

    entries = dj.transactions_for_object(message)
    assert [(t.kind, t.amount) for t in entries] == [
        (TransactionKind.CONSUMPTION, Decimal("-3")),
        (TransactionKind.STORNO, Decimal("3")),
    ]
    assert entries[1].storno_of_id == entries[0].id
    assert sum(t.amount for t in entries) == Decimal("0")


def test_shortcut_returns_credit_and_transfer_entries_attached_to_an_instance(dj, alice, message):
    guide = User.objects.create(username="guide")
    budget, pocket = _opened(dj, guide, "100"), _opened(dj, alice, "0")
    dj.credit(budget, "5", obj=message, actor=guide)
    dj.transfer(budget, pocket, "10", obj=message)

    entries = dj.transactions_for_object(message)

    assert [(t.wallet_id, t.kind, t.amount) for t in entries] == [
        (budget.id, TransactionKind.GRANT, Decimal("5")),
        (budget.id, TransactionKind.TRANSFER, Decimal("-10")),
        (pocket.id, TransactionKind.TRANSFER, Decimal("10")),
    ]
    assert entries[0].actor == subject_ref(guide)
    assert {t.obj for t in entries} == {object_ref(message)}


def test_shortcut_returns_nothing_for_an_instance_nothing_was_attached_to(dj, alice, message):
    _opened(dj, alice, "10")

    assert dj.transactions_for_object(message) == []
