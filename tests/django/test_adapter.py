"""
What only the Django adapter can be asked about: turning model instances into references,
the shortcut API, database-level guarantees, and the archive tables.

Engine behaviour is not retested here — ``tests/test_engine_*.py`` already runs all of it against
``DjangoStore``.
"""

import uuid

from decimal import Decimal

import pytest

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction as db_transaction
from django.test import override_settings

from allowances.types import ObjectRef, SubjectRef, TransactionKind, WalletSpec, WalletType
from allowances_django import shortcuts
from allowances_django.models import (
    FulfillmentRecord,
    Transaction,
    TransactionArchive,
    Wallet,
    WalletArchive,
)
from allowances_django.providers import SettingsProvider
from allowances_django.store import DjangoStore
from allowances_django.subjects import object_ref, resolve, subject_ref, subject_refs

pytestmark = pytest.mark.django_db

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


def test_a_model_instance_becomes_an_app_label_dot_model_reference(alice):
    assert subject_ref(alice) == SubjectRef("auth.user", str(alice.pk))


def test_an_existing_reference_passes_through_unchanged():
    ref = SubjectRef("auth.user", 7)

    assert subject_ref(ref) is ref


def test_a_whole_subjects_mapping_converts_at_once(alice):
    assert subject_refs({"device": alice}) == {"device": SubjectRef("auth.user", alice.pk)}


def test_an_unsaved_instance_cannot_be_referenced():
    with pytest.raises(ValueError, match="unsaved"):
        subject_ref(User(username="nobody"))


def test_something_that_is_not_a_model_is_rejected():
    with pytest.raises(TypeError):
        subject_ref("auth.user:1")


def test_resolve_returns_the_object_behind_a_reference(alice):
    assert resolve(subject_ref(alice)) == alice


def test_resolve_returns_none_for_a_subject_that_has_been_deleted(alice):
    ref = subject_ref(alice)
    alice.delete()

    assert resolve(ref) is None


def test_resolve_returns_none_for_a_model_that_no_longer_exists():
    assert resolve(SubjectRef("nonexistent.thing", 1)) is None


def test_object_ref_works_the_same_way(alice):
    assert object_ref(alice) == ObjectRef("auth.user", str(alice.pk))


# -- shortcuts ------------------------------------------------------------------------------------


@override_settings(ALLOWANCES=CONFIG)
def test_shortcuts_accept_model_instances_and_read_definitions_from_settings(alice):
    shortcuts.plan_switch({"device": alice}, [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("10"))])

    result = shortcuts.fulfill("send_email", 4, {"device": alice})

    assert result.satisfied is True
    assert Wallet.objects.get(subject_id=str(alice.pk)).balance == Decimal("6")


@override_settings(ALLOWANCES=CONFIG)
def test_shortcut_peek_writes_nothing(alice):
    shortcuts.plan_switch({"device": alice}, [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("10"))])

    result = shortcuts.peek("send_email", 4, {"device": alice})

    assert result.satisfied is True
    assert Transaction.objects.count() == 0


@override_settings(ALLOWANCES=CONFIG)
def test_shortcut_attach_takes_a_model_instance(alice):
    target = User.objects.create(username="the-thing-that-was-paid-for")
    shortcuts.plan_switch({"device": alice}, [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("10"))])
    result = shortcuts.fulfill("send_email", 3, {"device": alice})

    shortcuts.attach(result.guid, target)

    txn = Transaction.objects.get(kind=TransactionKind.CONSUMPTION)
    assert txn.object_type == "auth.user"
    assert txn.object_id == str(target.pk)


@override_settings(ALLOWANCES=CONFIG)
def test_unattached_records_are_findable(alice):
    shortcuts.plan_switch({"device": alice}, [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("10"))])
    result = shortcuts.fulfill("send_email", 3, {"device": alice})

    assert [r.guid for r in shortcuts.find_unattached_fulfillment_records()] == [result.guid]

    shortcuts.attach(result.guid, alice)
    assert shortcuts.find_unattached_fulfillment_records() == []


def test_the_settings_provider_falls_back_to_an_empty_config_when_the_setting_is_absent():
    from allowances.errors import DefinitionNotFoundError

    with override_settings():
        del_settings = SettingsProvider()
        with pytest.raises(DefinitionNotFoundError):
            del_settings.get_feature("send_email")


# -- database-level guarantees --------------------------------------------------------------------


def test_a_transaction_can_only_be_stornoed_once(alice, provider):
    store = DjangoStore()
    wallet = store.create_wallet(
        subject=subject_ref(alice), wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
    )
    from allowances.engine import fulfill, unfulfill

    result = fulfill(store, "send_email", 3, {"device": subject_ref(alice)}, provider)
    unfulfill(store, result.guid)

    with pytest.raises(IntegrityError):
        with db_transaction.atomic():
            Transaction.objects.create(
                wallet_id=wallet.id,
                kind=TransactionKind.STORNO,
                amount=Decimal("3"),
                storno_of_id=result.transactions[0].id,
            )


def test_subject_columns_hold_a_uuid_primary_key_without_complaint():
    # The old ContentType schema stored object ids in a PositiveBigIntegerField, which ruled this out.
    store = DjangoStore()
    subject = SubjectRef("billing.account", uuid.uuid4())

    wallet = store.create_wallet(
        subject=subject, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("1")
    )

    assert store.get_wallet(wallet.id).subject == subject


def test_deleting_a_wallet_takes_its_transactions_with_it(alice, provider):
    store = DjangoStore()
    wallet = store.create_wallet(
        subject=subject_ref(alice), wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
    )
    from allowances.engine import fulfill

    fulfill(store, "send_email", 3, {"device": subject_ref(alice)}, provider)

    Wallet.objects.filter(pk=wallet.id).delete()

    assert Transaction.objects.filter(wallet_id=wallet.id).count() == 0


def test_a_fulfillment_record_survives_its_transactions_being_archived(alice, provider):
    store = DjangoStore()
    store.create_wallet(
        subject=subject_ref(alice), wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("3")
    )
    from allowances.engine import fulfill

    result = fulfill(store, "send_email", 3, {"device": subject_ref(alice)}, provider)

    assert FulfillmentRecord.objects.filter(guid=result.guid).exists()
    assert TransactionArchive.objects.get().record_id == FulfillmentRecord.objects.get(guid=result.guid).pk


# -- archive tables -------------------------------------------------------------------------------


def test_archived_transactions_keep_pointing_at_the_entries_they_reversed(alice, provider):
    from dataclasses import replace

    from allowances.engine import archive_expired_wallets, fulfill, unfulfill
    from allowances.windows import utcnow

    store = DjangoStore()
    wallet = store.create_wallet(
        subject=subject_ref(alice), wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
    )
    result = fulfill(store, "send_email", 3, {"device": subject_ref(alice)}, provider)
    unfulfill(store, result.guid)

    now = utcnow()
    store.save_wallet(replace(store.get_wallet(wallet.id), valid_until=now))
    assert archive_expired_wallets(store, at=now) == 1

    archive = WalletArchive.objects.get(original_wallet_id=wallet.id)
    original = TransactionArchive.objects.get(wallet_archive=archive, storno_of__isnull=True)
    storno = TransactionArchive.objects.get(wallet_archive=archive, storno_of__isnull=False)
    assert storno.storno_of_id == original.id
    assert original.amount == Decimal("-3")
    assert storno.amount == Decimal("3")


def test_the_archive_records_when_the_wallet_was_originally_created(alice, provider):
    from allowances.engine import fulfill

    store = DjangoStore()
    wallet = store.create_wallet(
        subject=subject_ref(alice), wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("3")
    )
    fulfill(store, "send_email", 3, {"device": subject_ref(alice)}, provider)

    archive = WalletArchive.objects.get(original_wallet_id=wallet.id)
    assert archive.original_created_at == wallet.created_at
    assert archive.subject_type == "auth.user"
    assert archive.subject_id == str(alice.pk)
