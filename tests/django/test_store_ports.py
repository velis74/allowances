"""
The ``DjangoStore`` port methods for wallet locking, the actor column and memberships, and the
migration that adds them.
"""

from dataclasses import replace
from decimal import Decimal

import pytest

from django.core.management import call_command
from django.db import connection, transaction as db_transaction

from allowances.types import ObjectRef, SubjectRef, TransactionKind, WalletType
from allowances_django.models import Membership, Transaction, TransactionArchive
from allowances_django.store import DjangoStore

pytestmark = pytest.mark.django_db

ALICE = SubjectRef("auth.user", 1)
BOB = SubjectRef("auth.user", 2)
EDITORS = SubjectRef("auth.group", 1)
STAFF = SubjectRef("auth.group", 2)


@pytest.fixture
def store():
    return DjangoStore()


def wallet(store, subject=ALICE, balance="10", wallet_type=WalletType.CONSUMABLE):
    return store.create_wallet(subject=subject, wallet_type=wallet_type, currency="credits", balance=Decimal(balance))


def test_the_models_and_the_migrations_agree():
    call_command("makemigrations", "allowances", "--check", "--dry-run", verbosity=0)


# -- lock_wallets ---------------------------------------------------------------------------------


def test_lock_wallets_returns_the_wallets_in_ascending_id_order(store):
    first, second, third = (wallet(store) for _ in range(3))

    with store.unit_of_work():
        locked = store.lock_wallets([third.id, first.id, second.id])

    assert [w.id for w in locked] == [first.id, second.id, third.id]


def test_lock_wallets_returns_the_current_state(store):
    created = wallet(store, balance="10")
    store.save_wallet(replace(created, balance=Decimal("3")))

    with store.unit_of_work():
        (locked,) = store.lock_wallets([created.id])

    assert locked.balance == Decimal("3")


def test_lock_wallets_omits_unknown_and_archived_wallets(store):
    live, archived = wallet(store), wallet(store)
    store.archive_wallet(archived)

    with store.unit_of_work():
        locked = store.lock_wallets([archived.id, live.id, 999_999])

    assert [w.id for w in locked] == [live.id]


def test_lock_wallets_returns_a_wallet_once_when_its_id_repeats(store):
    created = wallet(store)

    with store.unit_of_work():
        locked = store.lock_wallets([created.id, created.id])

    assert [w.id for w in locked] == [created.id]


def test_lock_wallets_accepts_ids_as_strings_and_integers_together(store):
    first, second = wallet(store), wallet(store)

    with store.unit_of_work():
        locked = store.lock_wallets([str(second.id), first.id, str(first.id), "not-an-id"])

    assert [w.id for w in locked] == [first.id, second.id]


def test_a_transfer_takes_a_wallet_id_given_as_a_string(store):
    from allowances_django.shortcuts import transfer

    source, target = wallet(store, balance="10"), wallet(store, subject=BOB, balance="0")

    transfer(str(source.id), target.id, Decimal("4"), store=store)

    assert store.get_wallet(source.id).balance == Decimal("6")
    assert store.get_wallet(target.id).balance == Decimal("4")


def test_lock_wallets_of_nothing_is_empty(store):
    with store.unit_of_work():
        assert store.lock_wallets([]) == []


# -- actor ----------------------------------------------------------------------------------------


def test_a_transaction_keeps_its_actor(store):
    created = wallet(store)

    txn = store.create_transaction(
        wallet_id=created.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-1"), actor=BOB
    )

    assert txn.actor == BOB
    row = Transaction.objects.get(pk=txn.id)
    assert (row.actor_type, row.actor_id) == ("auth.user", "2")
    assert (
        store.transactions_for_wallet(wallet_id=created.id, since=row.created_at, until=row.created_at)[0].actor == BOB
    )


def test_a_transaction_without_an_actor_has_none(store):
    created = wallet(store)

    txn = store.create_transaction(wallet_id=created.id, kind=TransactionKind.GRANT, amount=Decimal("1"))

    assert txn.actor is None
    assert Transaction.objects.get(pk=txn.id).actor_type == ""


def test_archiving_copies_the_actor(store):
    created = wallet(store)
    txn = store.create_transaction(
        wallet_id=created.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-1"), actor=BOB
    )

    store.archive_wallet(created)

    archived = TransactionArchive.objects.get(original_transaction_id=txn.id)
    assert (archived.actor_type, archived.actor_id) == ("auth.user", "2")


def test_a_transfer_entry_is_stored_with_its_kind(store):
    created = wallet(store)

    txn = store.create_transaction(wallet_id=created.id, kind=TransactionKind.TRANSFER, amount=Decimal("-2"))

    assert txn.kind == TransactionKind.TRANSFER
    assert Transaction.objects.get(pk=txn.id).kind == "transfer"


# -- unattached records ---------------------------------------------------------------------------


def test_only_unattached_consumption_makes_a_record_unattached(store):
    created = wallet(store)
    consumed = store.get_or_create_record()
    store.create_transaction(
        wallet_id=created.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-1"), record_guid=consumed.guid
    )
    transferred = store.get_or_create_record()
    store.create_transaction(
        wallet_id=created.id, kind=TransactionKind.TRANSFER, amount=Decimal("-1"), record_guid=transferred.guid
    )
    credited = store.get_or_create_record()
    store.create_transaction(
        wallet_id=created.id, kind=TransactionKind.GRANT, amount=Decimal("1"), record_guid=credited.guid
    )

    assert [r.guid for r in store.records_with_unattached_transactions()] == [consumed.guid]


def test_an_attached_consumption_is_not_unattached(store):
    created = wallet(store)
    record = store.get_or_create_record()
    txn = store.create_transaction(
        wallet_id=created.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-1"), record_guid=record.guid
    )
    store.create_transaction(
        wallet_id=created.id, kind=TransactionKind.TRANSFER, amount=Decimal("-1"), record_guid=record.guid
    )

    store.attach_object(transaction_ids=[txn.id], obj=ObjectRef("mail.message", 1))

    assert store.records_with_unattached_transactions() == []


# -- memberships ----------------------------------------------------------------------------------


def test_memberships_of_lists_direct_pairs_of_the_given_members(store):
    store.add_membership(member=ALICE, group=EDITORS)
    store.add_membership(member=EDITORS, group=STAFF)
    store.add_membership(member=BOB, group=STAFF)

    assert store.memberships_of(members=[ALICE]) == [(ALICE, EDITORS)]
    assert store.memberships_of(members=[ALICE, EDITORS]) == [(ALICE, EDITORS), (EDITORS, STAFF)]
    assert store.memberships_of(members=[]) == []


def test_adding_an_existing_membership_stores_it_once(store):
    store.add_membership(member=ALICE, group=EDITORS)
    store.add_membership(member=ALICE, group=EDITORS)

    assert Membership.objects.count() == 1


def test_removing_a_membership_reports_whether_there_was_one(store):
    store.add_membership(member=ALICE, group=EDITORS)

    assert store.remove_membership(member=ALICE, group=EDITORS) is True
    assert store.remove_membership(member=ALICE, group=EDITORS) is False
    assert store.memberships_of(members=[ALICE]) == []


def test_lock_memberships_runs_inside_a_unit_of_work(store):
    with store.unit_of_work():
        store.lock_memberships()
        store.add_membership(member=ALICE, group=EDITORS)

    assert store.memberships_of(members=[ALICE]) == [(ALICE, EDITORS)]


@pytest.mark.skipif(connection.vendor != "postgresql", reason="the advisory lock exists only on PostgreSQL")
def test_lock_memberships_holds_an_advisory_lock_until_the_transaction_ends(store):
    with db_transaction.atomic():
        store.lock_memberships()
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
            assert cursor.fetchone()[0] == 1
