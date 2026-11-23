"""
The port methods for wallet locking, the actor column, the archive tables and memberships of the stores that keep their
data in the tables of ``allowances_django`` (``db_store``), and the migration that adds them.
"""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from django.core.management import call_command

from allowances.types import ObjectRef, SubjectRef, TransactionKind, WalletType
from allowances.windows import utcnow
from allowances_django import shortcuts
from allowances_django.models import FulfillmentRecord, Membership, Transaction, TransactionArchive, WalletArchive
from allowances_django.sync import shortcuts as sync_shortcuts
from tests.api import archive_expired_wallets, fulfill, ThreadedSyncStore, unfulfill

ALICE = SubjectRef("auth.user", 1)
BOB = SubjectRef("auth.user", 2)
EDITORS = SubjectRef("auth.group", 1)
STAFF = SubjectRef("auth.group", 2)


@pytest.fixture
def store(db_store):
    return db_store


async def wallet(store, subject=ALICE, balance="10", wallet_type=WalletType.CONSUMABLE):
    return await store.create_wallet(
        subject=subject, wallet_type=wallet_type, currency="credits", balance=Decimal(balance)
    )


@pytest.mark.django_db
def test_the_models_and_the_migrations_agree():
    call_command("makemigrations", "allowances", "--check", "--dry-run", verbosity=0)


# -- lock_wallets ---------------------------------------------------------------------------------


async def test_lock_wallets_returns_the_wallets_in_ascending_id_order(store):
    first, second, third = [await wallet(store) for _ in range(3)]

    async with store.unit_of_work():
        locked = await store.lock_wallets([third.id, first.id, second.id])

    assert [w.id for w in locked] == [first.id, second.id, third.id]


async def test_lock_wallets_returns_the_current_state(store):
    created = await wallet(store, balance="10")
    await store.save_wallet(replace(created, balance=Decimal("3")))

    async with store.unit_of_work():
        (locked,) = await store.lock_wallets([created.id])

    assert locked.balance == Decimal("3")


async def test_lock_wallets_omits_unknown_and_archived_wallets(store):
    live, archived = await wallet(store), await wallet(store)
    await store.archive_wallet(archived)

    async with store.unit_of_work():
        locked = await store.lock_wallets([archived.id, live.id, 999_999])

    assert [w.id for w in locked] == [live.id]


async def test_lock_wallets_returns_a_wallet_once_when_its_id_repeats(store):
    created = await wallet(store)

    async with store.unit_of_work():
        locked = await store.lock_wallets([created.id, created.id])

    assert [w.id for w in locked] == [created.id]


async def test_lock_wallets_accepts_ids_as_strings_and_integers_together(store):
    first, second = await wallet(store), await wallet(store)

    async with store.unit_of_work():
        locked = await store.lock_wallets([str(second.id), first.id, str(first.id), "not-an-id"])

    assert [w.id for w in locked] == [first.id, second.id]


async def test_a_transfer_takes_a_wallet_id_given_as_a_string(store):
    source, target = await wallet(store, balance="10"), await wallet(store, subject=BOB, balance="0")

    if isinstance(store, ThreadedSyncStore):
        await store.call(sync_shortcuts.transfer, str(source.id), target.id, Decimal("4"), store=store.store)
    else:
        await shortcuts.transfer(str(source.id), target.id, Decimal("4"), store=store)

    assert (await store.get_wallet(source.id)).balance == Decimal("6")
    assert (await store.get_wallet(target.id)).balance == Decimal("4")


async def test_lock_wallets_of_nothing_is_empty(store):
    async with store.unit_of_work():
        assert await store.lock_wallets([]) == []


# -- actor ----------------------------------------------------------------------------------------


async def test_a_transaction_keeps_its_actor(store):
    created = await wallet(store)

    txn = await store.create_transaction(
        wallet_id=created.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-1"), actor=BOB
    )

    assert txn.actor == BOB
    row = await Transaction.objects.aget(pk=txn.id)
    assert (row.actor_type, row.actor_id) == ("auth.user", "2")
    assert (await store.transactions_for_wallet(wallet_id=created.id, since=row.created_at, until=row.created_at))[
        0
    ].actor == BOB


async def test_a_transaction_without_an_actor_has_none(store):
    created = await wallet(store)

    txn = await store.create_transaction(wallet_id=created.id, kind=TransactionKind.GRANT, amount=Decimal("1"))

    assert txn.actor is None
    assert (await Transaction.objects.aget(pk=txn.id)).actor_type == ""


async def test_archiving_copies_the_actor(store):
    created = await wallet(store)
    txn = await store.create_transaction(
        wallet_id=created.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-1"), actor=BOB
    )

    await store.archive_wallet(created)

    archived = await TransactionArchive.objects.aget(original_transaction_id=txn.id)
    assert (archived.actor_type, archived.actor_id) == ("auth.user", "2")


async def test_archived_transactions_keep_pointing_at_the_entries_they_reversed(store, provider):
    created = await wallet(store, subject=ALICE)
    result = await fulfill(store, "send_email", 3, {"device": ALICE}, provider)
    await unfulfill(store, result.guid)
    now = utcnow()
    await store.save_wallet(replace(await store.get_wallet(created.id), valid_until=now - timedelta(seconds=1)))

    assert await archive_expired_wallets(store, at=now) == 1

    archive = await WalletArchive.objects.aget(original_wallet_id=created.id)
    original = await TransactionArchive.objects.aget(wallet_archive=archive, storno_of__isnull=True)
    storno = await TransactionArchive.objects.aget(wallet_archive=archive, storno_of__isnull=False)
    assert storno.storno_of_id == original.id
    assert (original.amount, storno.amount) == (Decimal("-3"), Decimal("3"))
    record = await FulfillmentRecord.objects.aget(guid=result.guid)
    assert (original.record_id, storno.record_id) == (record.pk, record.pk)


async def test_a_fulfillment_record_survives_its_transactions_being_archived(store, provider):
    await wallet(store, subject=ALICE, balance="3")

    result = await fulfill(store, "send_email", 3, {"device": ALICE}, provider)

    record = await FulfillmentRecord.objects.aget(guid=result.guid)
    assert (await TransactionArchive.objects.aget()).record_id == record.pk


async def test_the_archive_records_when_the_wallet_was_originally_created(store, provider):
    created = await wallet(store, subject=ALICE, balance="3")

    await fulfill(store, "send_email", 3, {"device": ALICE}, provider)

    archive = await WalletArchive.objects.aget(original_wallet_id=created.id)
    assert archive.original_created_at == created.created_at
    assert (archive.subject_type, archive.subject_id) == ("auth.user", "1")


async def test_a_transfer_entry_is_stored_with_its_kind(store):
    created = await wallet(store)

    txn = await store.create_transaction(wallet_id=created.id, kind=TransactionKind.TRANSFER, amount=Decimal("-2"))

    assert txn.kind == TransactionKind.TRANSFER
    assert (await Transaction.objects.aget(pk=txn.id)).kind == "transfer"


# -- unattached records ---------------------------------------------------------------------------


async def test_only_unattached_consumption_makes_a_record_unattached(store):
    created = await wallet(store)
    consumed = await store.get_or_create_record()
    await store.create_transaction(
        wallet_id=created.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-1"), record_guid=consumed.guid
    )
    transferred = await store.get_or_create_record()
    await store.create_transaction(
        wallet_id=created.id, kind=TransactionKind.TRANSFER, amount=Decimal("-1"), record_guid=transferred.guid
    )
    credited = await store.get_or_create_record()
    await store.create_transaction(
        wallet_id=created.id, kind=TransactionKind.GRANT, amount=Decimal("1"), record_guid=credited.guid
    )

    assert [r.guid for r in await store.records_with_unattached_transactions()] == [consumed.guid]


async def test_an_attached_consumption_is_not_unattached(store):
    created = await wallet(store)
    record = await store.get_or_create_record()
    txn = await store.create_transaction(
        wallet_id=created.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-1"), record_guid=record.guid
    )
    await store.create_transaction(
        wallet_id=created.id, kind=TransactionKind.TRANSFER, amount=Decimal("-1"), record_guid=record.guid
    )

    await store.attach_object(transaction_ids=[txn.id], obj=ObjectRef("mail.message", 1))

    assert await store.records_with_unattached_transactions() == []


# -- memberships ----------------------------------------------------------------------------------


async def test_memberships_of_lists_direct_pairs_of_the_given_members(store):
    await store.add_membership(member=ALICE, group=EDITORS)
    await store.add_membership(member=EDITORS, group=STAFF)
    await store.add_membership(member=BOB, group=STAFF)

    assert await store.memberships_of(members=[ALICE]) == [(ALICE, EDITORS)]
    assert await store.memberships_of(members=[ALICE, EDITORS]) == [(ALICE, EDITORS), (EDITORS, STAFF)]
    assert await store.memberships_of(members=[]) == []


async def test_adding_an_existing_membership_stores_it_once(store):
    await store.add_membership(member=ALICE, group=EDITORS)
    await store.add_membership(member=ALICE, group=EDITORS)

    assert await Membership.objects.acount() == 1


async def test_removing_a_membership_reports_whether_there_was_one(store):
    await store.add_membership(member=ALICE, group=EDITORS)

    assert await store.remove_membership(member=ALICE, group=EDITORS) is True
    assert await store.remove_membership(member=ALICE, group=EDITORS) is False
    assert await store.memberships_of(members=[ALICE]) == []


async def test_lock_memberships_runs_inside_a_unit_of_work(store):
    async with store.unit_of_work():
        await store.lock_memberships()
        await store.add_membership(member=ALICE, group=EDITORS)

    assert await store.memberships_of(members=[ALICE]) == [(ALICE, EDITORS)]
