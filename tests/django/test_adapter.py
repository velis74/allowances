"""
Django adapter specifics: model instances to references, the shortcut API and database-level guarantees.

Engine behaviour is tested in ``tests/test_engine_*.py``, which runs against the Django stores too. The tests of the
shortcuts and the stores run once per flavour (``dj``): synchronous and asynchronous.
"""

import uuid

from decimal import Decimal

import pytest

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction as db_transaction
from django.test import override_settings
from django.utils import timezone

from allowances.errors import InsufficientBalanceError
from allowances.types import ObjectRef, SubjectRef, SwitchPolicy, TransactionKind, WalletSpec, WalletType
from allowances_django import shortcuts
from allowances_django.models import (
    Transaction,
    Wallet,
)
from allowances_django.providers import SettingsProvider
from allowances_django.store import DjangoStore
from allowances_django.subjects import object_ref, resolve, subject_ref, subject_refs

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
def provider():
    return SettingsProvider(config=CONFIG)


# -- references -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_model_instance_becomes_an_app_label_dot_model_reference(alice):
    assert subject_ref(alice) == SubjectRef("auth.user", str(alice.pk))


@pytest.mark.django_db
def test_an_existing_reference_passes_through_unchanged():
    ref = SubjectRef("auth.user", 7)

    assert subject_ref(ref) is ref


@pytest.mark.django_db
def test_a_whole_subjects_mapping_converts_at_once(alice):
    assert subject_refs({"device": alice}) == {"device": SubjectRef("auth.user", alice.pk)}


@pytest.mark.django_db
def test_an_unsaved_instance_cannot_be_referenced():
    with pytest.raises(ValueError, match="unsaved"):
        subject_ref(User(username="nobody"))


@pytest.mark.django_db
def test_something_that_is_not_a_model_is_rejected():
    with pytest.raises(TypeError):
        subject_ref("auth.user:1")


@pytest.mark.django_db
def test_resolve_returns_the_object_behind_a_reference(alice):
    assert resolve(subject_ref(alice)) == alice


@pytest.mark.django_db
def test_resolve_returns_none_for_a_subject_that_has_been_deleted(alice):
    ref = subject_ref(alice)
    alice.delete()

    assert resolve(ref) is None


@pytest.mark.django_db
def test_resolve_returns_none_for_a_model_that_no_longer_exists():
    assert resolve(SubjectRef("nonexistent.thing", 1)) is None


@pytest.mark.django_db
def test_object_ref_works_the_same_way(alice):
    assert object_ref(alice) == ObjectRef("auth.user", str(alice.pk))


# -- shortcuts ------------------------------------------------------------------------------------


@override_settings(ALLOWANCES=CONFIG)
def test_shortcuts_accept_model_instances_and_read_definitions_from_settings(dj, alice):
    dj.plan_switch({"device": alice}, [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("10"))])

    result = dj.fulfill("send_email", 4, {"device": alice})

    assert result.satisfied is True
    assert Wallet.objects.get(subject_id=str(alice.pk)).balance == Decimal("6")


@override_settings(ALLOWANCES=CONFIG)
def test_shortcut_peek_writes_nothing(dj, alice):
    dj.plan_switch({"device": alice}, [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("10"))])

    result = dj.peek("send_email", 4, {"device": alice})

    assert result.satisfied is True
    assert Transaction.objects.count() == 0


@override_settings(ALLOWANCES=CONFIG)
def test_shortcut_attach_takes_a_model_instance(dj, alice):
    target = User.objects.create(username="the-thing-that-was-paid-for")
    dj.plan_switch({"device": alice}, [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("10"))])
    result = dj.fulfill("send_email", 3, {"device": alice})

    dj.attach(result.guid, target)

    txn = Transaction.objects.get(kind=TransactionKind.CONSUMPTION)
    assert txn.object_type == "auth.user"
    assert txn.object_id == str(target.pk)


@override_settings(ALLOWANCES=CONFIG)
def test_shortcuts_open_credit_adjust_and_report_with_model_instances(dj, alice):
    payment = User.objects.create(username="stands-in-for-a-payment")
    spec = WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("0"), on_switch=SwitchPolicy.ADJUST)
    wallet = dj.open_wallet(alice, spec, source="purchase")

    dj.credit(wallet, "10", obj=payment)
    dj.fulfill("send_email", 3, {"device": alice})
    adjusted = dj.adjust_wallet(Wallet.objects.get(pk=wallet.id), "5")

    row = Wallet.objects.get(pk=wallet.id)
    assert (row.source, row.balance, row.allotment) == ("purchase", Decimal("12"), Decimal("5"))
    assert adjusted.balance == Decimal("12")
    grant = Transaction.objects.filter(kind=TransactionKind.GRANT).order_by("id").first()
    assert (grant.object_type, grant.object_id) == ("auth.user", str(payment.pk))
    assert dj.consumed(wallet.id, row.created_at, timezone.now()) == Decimal("3")


@override_settings(ALLOWANCES=CONFIG)
def test_unattached_records_are_findable(dj, alice):
    dj.plan_switch({"device": alice}, [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("10"))])
    result = dj.fulfill("send_email", 3, {"device": alice})

    assert [r.guid for r in dj.find_unattached_fulfillment_records()] == [result.guid]

    dj.attach(result.guid, alice)
    assert dj.find_unattached_fulfillment_records() == []


@pytest.mark.django_db
def test_the_settings_provider_falls_back_to_an_empty_config_when_the_setting_is_absent():
    from allowances.errors import DefinitionNotFoundError

    with override_settings():
        del_settings = SettingsProvider()
        with pytest.raises(DefinitionNotFoundError):
            del_settings.get_feature("send_email")


# -- database-level guarantees --------------------------------------------------------------------


def test_a_transaction_can_only_be_stornoed_once(dj, alice, provider):
    store = dj.store()
    wallet = dj.run(
        store.create_wallet(
            subject=subject_ref(alice), wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
        )
    )

    result = dj.engine.fulfill(store, "send_email", 3, {"device": subject_ref(alice)}, provider)
    dj.engine.unfulfill(store, result.guid)

    with pytest.raises(IntegrityError):
        with db_transaction.atomic():
            Transaction.objects.create(
                wallet_id=wallet.id,
                kind=TransactionKind.STORNO,
                amount=Decimal("3"),
                storno_of_id=result.transactions[0].id,
            )


def test_subject_columns_hold_a_uuid_primary_key_without_complaint(dj):
    # Subject ids are stored as text, so a UUID primary key fits.
    store = dj.store()
    subject = SubjectRef("billing.account", uuid.uuid4())

    wallet = dj.run(
        store.create_wallet(
            subject=subject, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("1")
        )
    )

    assert dj.run(store.get_wallet(wallet.id)).subject == subject


def test_deleting_a_wallet_takes_its_transactions_with_it(dj, alice, provider):
    store = dj.store()
    wallet = dj.run(
        store.create_wallet(
            subject=subject_ref(alice), wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
        )
    )

    dj.engine.fulfill(store, "send_email", 3, {"device": subject_ref(alice)}, provider)

    Wallet.objects.filter(pk=wallet.id).delete()

    assert Transaction.objects.filter(wallet_id=wallet.id).count() == 0


# -- transfer, close_wallet and the actor through the shortcuts -----------------------------------


def _opened(dj, subject, balance):
    return dj.open_wallet(subject, WalletSpec(WalletType.CONSUMABLE, "credits", Decimal(balance)), source="budget")


def test_shortcut_transfer_takes_wallet_rows_and_records_obj_and_actor(dj, alice):
    guide = User.objects.create(username="guide")
    trip = User.objects.create(username="stands-in-for-a-trip")
    budget = Wallet.objects.get(pk=_opened(dj, guide, "100").id)
    pocket = _opened(dj, alice, "0")

    result = dj.transfer(budget, pocket, "10", obj=trip, actor=guide)

    legs = Transaction.objects.filter(record__guid=result.guid).order_by("id")
    assert [(t.wallet_id, t.kind, t.amount) for t in legs] == [
        (budget.pk, TransactionKind.TRANSFER, Decimal("-10")),
        (pocket.id, TransactionKind.TRANSFER, Decimal("10")),
    ]
    assert {(t.actor_type, t.actor_id) for t in legs} == {("auth.user", str(guide.pk))}
    assert {(t.object_type, t.object_id) for t in legs} == {("auth.user", str(trip.pk))}
    assert Wallet.objects.get(pk=pocket.id).balance == Decimal("10")


def test_shortcut_transfer_takes_wallet_ids(dj, alice):
    guide = User.objects.create(username="guide")
    budget, pocket = _opened(dj, guide, "100"), _opened(dj, alice, "0")

    dj.transfer(budget.id, pocket.id, "4")

    assert Wallet.objects.get(pk=budget.id).balance == Decimal("96")
    assert Wallet.objects.get(pk=pocket.id).balance == Decimal("4")


def test_shortcut_close_wallet_takes_a_wallet_row(dj, alice):
    row = Wallet.objects.get(pk=_opened(dj, alice, "5").id)
    now = timezone.now()

    closed = dj.close_wallet(row, at=now)

    assert closed.valid_until == now
    assert Wallet.objects.get(pk=row.pk).valid_until == now
    assert not Transaction.objects.filter(wallet_id=row.pk).exists()


def test_shortcut_credit_and_unfulfill_record_a_model_actor(dj, alice):
    clerk = User.objects.create(username="clerk")
    wallet = _opened(dj, alice, "10")
    guid = uuid.uuid4()

    dj.credit(wallet, "5", guid=guid, actor=alice)
    dj.unfulfill(guid, actor=clerk)

    grant = Transaction.objects.get(kind=TransactionKind.GRANT)
    storno = Transaction.objects.get(kind=TransactionKind.STORNO)
    assert (grant.actor_type, grant.actor_id) == ("auth.user", str(alice.pk))
    assert (storno.actor_type, storno.actor_id) == ("auth.user", str(clerk.pk))
    assert Wallet.objects.get(pk=wallet.id).balance == Decimal("10")


def _hand_out(dj, guide_budget, tourist, guide):
    """Open a wallet and fund it in one atomic block: ``atomic()`` (sync) or the store's unit of work (async)."""
    if dj.is_async:
        return async_to_sync(_hand_out_async)(guide_budget, tourist, guide)
    with db_transaction.atomic():
        pocket = dj.open_wallet(tourist, WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("0")), source="allowance")
        dj.transfer(guide_budget, pocket, Decimal("10"), actor=guide)
    return pocket


async def _hand_out_async(guide_budget, tourist, guide):
    store = DjangoStore()
    async with store.unit_of_work():
        pocket = await shortcuts.open_wallet(
            tourist, WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("0")), source="allowance", store=store
        )
        await shortcuts.transfer(guide_budget, pocket, Decimal("10"), actor=guide, store=store)
    return pocket


def test_the_hand_out_recipe_opens_and_funds_a_wallet(dj, alice):
    guide = User.objects.create(username="guide")
    budget = _opened(dj, guide, "15")

    pocket = _hand_out(dj, budget, alice, guide)

    assert Wallet.objects.get(pk=pocket.id).balance == Decimal("10")
    assert Wallet.objects.get(pk=budget.id).balance == Decimal("5")
    assert Transaction.objects.filter(kind=TransactionKind.TRANSFER).count() == 2


def test_the_hand_out_recipe_leaves_no_wallet_when_the_budget_is_short(dj, alice):
    guide = User.objects.create(username="guide")
    budget = _opened(dj, guide, "5")

    with pytest.raises(InsufficientBalanceError):
        _hand_out(dj, budget, alice, guide)

    assert list(Wallet.objects.values_list("pk", flat=True)) == [budget.id]
    assert not Transaction.objects.exists()
