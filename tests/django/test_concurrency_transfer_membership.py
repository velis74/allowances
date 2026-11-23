"""
Concurrent transfers, transfers and reversals alongside fulfilment, and concurrent membership
changes: no deadlock, exact balances, and no membership cycle stored. Each test runs once per store that
keeps its data in the database (``store_factory``).
"""

import asyncio
import threading

from decimal import Decimal

import pytest

from asgiref.sync import sync_to_async
from django.db import connection, connections, DatabaseError, OperationalError, transaction as db_transaction

import allowances.sync

from allowances.errors import InsufficientBalanceError, MembershipCycleError
from allowances.types import SubjectRef, TransactionKind
from allowances_django.models import AllowancesLock, Membership, Transaction, TransactionArchive, Wallet
from tests.api import add_member, credit, fulfill, transfer, unfulfill
from tests.conftest import CREDITS, provider_with
from tests.django.test_concurrency import run_concurrently, WORKERS

ROUNDS = 20


async def open_funded(store, subject, balance="1000"):
    return await store.create_wallet(
        subject=subject,
        wallet_type="consumable",
        currency="credits",
        balance=Decimal(balance),
        overdraft_limit=Decimal("0"),
    )


async def balance(wallet):
    return (await Wallet.objects.aget(pk=wallet.id)).balance


async def ledger_sum(wallet):
    return sum([t.amount async for t in Transaction.objects.filter(wallet_id=wallet.id)], Decimal("0"))


async def test_transfers_in_opposite_directions_do_not_deadlock(store_factory):
    store = store_factory()
    first = await open_funded(store, SubjectRef("auth.user", 1), "100")
    second = await open_funded(store, SubjectRef("auth.user", 2), "100")
    stores = [store_factory(index) for index in range(WORKERS)]

    async def move(index):
        source, target = (first, second) if index % 2 else (second, first)
        for _ in range(ROUNDS):
            await transfer(stores[index], source.id, target.id, Decimal("1"))

    outcomes = await run_concurrently(move)

    errors = [o for o in outcomes if isinstance(o, Exception)]
    assert not errors, f"concurrent transfers raised: {errors}"
    assert await balance(first) == Decimal("100")
    assert await balance(second) == Decimal("100")
    assert await ledger_sum(first) == await ledger_sum(second) == Decimal("0")
    assert await Transaction.objects.filter(kind=TransactionKind.TRANSFER).acount() == 2 * 8 * ROUNDS


def _two_orders_provider():
    return provider_with(
        features={
            "user_first": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"user", "team"},
                "fulfillment_order": [("consumable", "user"), ("consumable", "team")],
            },
            "team_first": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"user", "team"},
                "fulfillment_order": [("consumable", "team"), ("consumable", "user")],
            },
        },
        currencies=CREDITS,
    )


async def test_fulfill_and_transfer_over_the_same_wallets_do_not_deadlock(store_factory):
    provider = _two_orders_provider()
    user, team = SubjectRef("auth.user", 1), SubjectRef("team", 1)
    store = store_factory()
    user_wallet, team_wallet = await open_funded(store, user), await open_funded(store, team)
    stores = [store_factory(index) for index in range(WORKERS)]

    async def work(index):
        granted = Decimal("0")
        mine = stores[index]
        for _ in range(ROUNDS):
            kind = index % 4
            if kind == 0:
                granted += (await fulfill(mine, "user_first", 3, {"user": user, "team": team}, provider)).granted
            elif kind == 1:
                granted += (await fulfill(mine, "team_first", 3, {"user": user, "team": team}, provider)).granted
            else:
                source, target = (user_wallet, team_wallet) if kind == 2 else (team_wallet, user_wallet)
                try:
                    await transfer(mine, source.id, target.id, Decimal("2"))
                except InsufficientBalanceError:
                    pass
        return granted

    outcomes = await run_concurrently(work)

    errors = [o for o in outcomes if isinstance(o, Exception)]
    assert not errors, f"concurrent fulfillment and transfer raised: {errors}"
    granted = sum(outcomes, Decimal("0"))
    assert granted == Decimal("3") * ROUNDS * 4
    assert await balance(user_wallet) == Decimal("1000") + await ledger_sum(user_wallet)
    assert await balance(team_wallet) == Decimal("1000") + await ledger_sum(team_wallet)
    assert await balance(user_wallet) + await balance(team_wallet) == Decimal("2000") - granted
    assert await balance(user_wallet) >= 0 and await balance(team_wallet) >= 0


async def test_unfulfill_and_fulfill_over_the_same_wallets_do_not_deadlock(store_factory):
    """Reversals and fulfilments over the same user and team wallets run concurrently without deadlock."""
    provider = _two_orders_provider()
    user, team = SubjectRef("auth.user", 1), SubjectRef("team", 1)
    store = store_factory()
    user_wallet, team_wallet = await open_funded(store, user), await open_funded(store, team, "0")
    reversers = 4
    guids = []
    for _ in range(reversers * ROUNDS):
        await credit(store, team_wallet.id, Decimal("1"))
        guids.append((await fulfill(store, "team_first", 3, {"user": user, "team": team}, provider)).guid)
    assert await balance(team_wallet) == Decimal("0")
    stores = [store_factory(index) for index in range(2 * reversers)]

    async def work(index):
        granted = Decimal("0")
        mine = stores[index]
        for round_ in range(ROUNDS):
            if index % 2:
                await unfulfill(mine, guids[(index // 2) * ROUNDS + round_])
            else:
                granted += (await fulfill(mine, "user_first", 3, {"user": user, "team": team}, provider)).granted
        return granted

    outcomes = await run_concurrently(work, count=2 * reversers)

    errors = [o for o in outcomes if isinstance(o, Exception)]
    assert not errors, f"concurrent fulfillment and reversal raised: {errors}"
    granted = sum(outcomes, Decimal("0"))
    assert granted == Decimal("3") * ROUNDS * reversers
    assert await Transaction.objects.filter(kind=TransactionKind.STORNO).acount() == 2 * reversers * ROUNDS
    assert await balance(user_wallet) == Decimal("1000") + await ledger_sum(user_wallet)
    assert await balance(team_wallet) == await ledger_sum(team_wallet)
    assert await balance(user_wallet) + await balance(team_wallet) == Decimal("1000") + reversers * ROUNDS - granted


async def test_unfulfill_alongside_a_fulfill_that_archives_its_wallet_does_not_deadlock(store_factory):
    provider = provider_with(
        features={
            "use": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"user"},
                "fulfillment_order": [("consumable", "user")],
            }
        },
        currencies=CREDITS,
    )
    store = store_factory()
    stores = [store_factory(index) for index in range(WORKERS)]
    for round_ in range(5):
        user = SubjectRef("auth.user", round_ + 1)
        wallet = await store.create_wallet(
            subject=user, wallet_type="consumable", currency="credits", balance=Decimal("40")
        )
        guids = [(await fulfill(store, "use", 1, {"user": user}, provider)).guid for _ in range(16)]

        async def work(index, user=user, guids=guids):
            if index % 2:
                result = await fulfill(stores[index], "use", 1000, {"user": user}, provider, allow_partial=True)
                return result.granted
            for guid in guids[index // 2 :: 4]:
                await unfulfill(stores[index], guid)
            return Decimal("0")

        outcomes = await run_concurrently(work)

        errors = [o for o in outcomes if isinstance(o, Exception)]
        assert not errors, f"unfulfill alongside archival raised: {errors}"
        stornos = await Transaction.objects.filter(wallet_id=wallet.id, kind=TransactionKind.STORNO).acount()
        stornos += await TransactionArchive.objects.filter(
            original_wallet_id=wallet.id, kind=TransactionKind.STORNO
        ).acount()
        live = await Wallet.objects.filter(pk=wallet.id).afirst()
        left = live.balance if live is not None else Decimal("0")
        assert Decimal("40") - 16 + stornos == sum(outcomes, Decimal("0")) + left


async def _opposite_memberships_store_no_cycle(store_factory, *, empty_lock_table):
    stores = [store_factory(index) for index in range(2)]
    for pair in range(10):
        if empty_lock_table:
            await AllowancesLock.objects.all().adelete()
        first, second = SubjectRef("auth.group", 2 * pair + 1), SubjectRef("auth.group", 2 * pair + 2)

        async def join(index, first=first, second=second):
            member, group = (first, second) if index % 2 else (second, first)
            await add_member(stores[index], member, group)

        outcomes = await run_concurrently(join, count=2)

        assert sum(o is None for o in outcomes) == 1, outcomes
        assert sum(isinstance(o, MembershipCycleError) for o in outcomes) == 1, outcomes
        stored = Membership.objects.filter(
            member_type="auth.group",
            member_id__in=[first.id, second.id],
            group_id__in=[first.id, second.id],
        )
        assert await stored.acount() == 1


async def test_opposite_memberships_added_at_once_store_no_cycle(store_factory):
    await _opposite_memberships_store_no_cycle(store_factory, empty_lock_table=False)


async def test_add_member_works_after_the_lock_table_was_emptied(store_factory):
    await AllowancesLock.objects.all().adelete()

    await add_member(store_factory(), SubjectRef("auth.user", 1), SubjectRef("auth.group", 1))

    assert await Membership.objects.filter(member_id=1, group_id=1).acount() == 1
    if connection.features.has_select_for_update:
        assert await AllowancesLock.objects.filter(name="membership").aexists()


async def test_opposite_memberships_added_at_once_after_the_lock_table_was_emptied_store_no_cycle(store_factory):
    await _opposite_memberships_store_no_cycle(store_factory, empty_lock_table=True)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="needs PostgreSQL REPEATABLE READ")
def test_lock_row_created_after_the_snapshot_under_repeatable_read_raises_then_a_retry_works():
    """
    The synchronous Django store joins the caller's ``atomic()`` block; under REPEATABLE READ a lock row
    committed after the block's snapshot fails the first ``add_member`` and a new block succeeds.
    """
    from allowances_django.sync.store import DjangoStore

    AllowancesLock.objects.all().delete()
    alice, editors = SubjectRef("auth.user", 1), SubjectRef("auth.group", 1)

    def create_lock_row_elsewhere():
        try:
            AllowancesLock.objects.create(name="membership")
        finally:
            connection.close()

    with pytest.raises(OperationalError), db_transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        assert not AllowancesLock.objects.exists()  # takes the snapshot
        other = threading.Thread(target=create_lock_row_elsewhere)
        other.start()
        other.join(10)
        allowances.sync.add_member(DjangoStore(), alice, editors)

    with db_transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        allowances.sync.add_member(DjangoStore(), alice, editors)

    assert Membership.objects.filter(member_id=1, group_id=1).count() == 1


ADORM_DRIVERS = {
    "adorm-django": "adorm.drivers.django.DjangoDriver",
    "adorm-asyncpg": "adorm.drivers.asyncpg.AsyncpgDriver",
}


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="needs PostgreSQL REPEATABLE READ")
@pytest.mark.parametrize("kind", list(ADORM_DRIVERS))
async def test_adorm_lock_row_created_after_the_snapshot_under_repeatable_read_raises_then_a_retry_works(kind):
    """
    ``AdormStore`` on a profile whose transactions run under REPEATABLE READ: a lock row committed after the unit of
    work's snapshot fails the first ``add_member`` with ``adorm.OperationalError`` and a new unit of work succeeds.
    """
    import adorm

    from adorm.settings import ConnectionProfile, register_profile, unregister_profile
    from django.conf import settings
    from psycopg import IsolationLevel

    from allowances_adorm.store import AdormStore
    from tests.adorm_support import configure_adorm

    configure_adorm()
    options = {**settings.DATABASES["default"].get("OPTIONS", {}), "isolation_level": IsolationLevel.REPEATABLE_READ}
    name = f"repeatable-read-{kind}"
    register_profile(ConnectionProfile(name, ADORM_DRIVERS[kind], alias="default", params={"OPTIONS": options}))
    try:
        await AllowancesLock.objects.all().adelete()
        store = AdormStore(name)
        alice, editors = SubjectRef("auth.user", 1), SubjectRef("auth.group", 1)

        with pytest.raises(adorm.OperationalError, match="not visible in this transaction"):
            async with store.unit_of_work():
                assert await store.memberships_of(members=[alice]) == []  # takes the snapshot
                await AllowancesLock.objects.acreate(name="membership")  # another connection, committed
                await add_member(store, alice, editors)

        await add_member(store, alice, editors)

        assert await Membership.objects.filter(member_id=1, group_id=1).acount() == 1
    finally:
        unregister_profile(name)
        await sync_to_async(connections.close_all)()


def _lock_row_is_free():
    """Take and release the membership lock row with ``NOWAIT``; raises ``DatabaseError`` while another holds it."""
    with db_transaction.atomic():
        return AllowancesLock.objects.select_for_update(nowait=True).get(name="membership")


@pytest.mark.skipif(not connection.features.has_select_for_update_nowait, reason="needs SELECT ... FOR UPDATE NOWAIT")
async def test_lock_memberships_holds_a_row_lock_until_the_unit_of_work_ends(store_factory):
    store = store_factory()
    async with store.unit_of_work():
        await store.lock_memberships()  # commits the lock row, so the other connection sees it
    held, release = asyncio.Event(), asyncio.Event()

    async def hold():
        holder_store = store_factory(1)
        async with holder_store.unit_of_work():
            await holder_store.lock_memberships()
            held.set()
            await asyncio.wait_for(release.wait(), 10)

    holder = asyncio.create_task(hold())
    try:
        await asyncio.wait_for(held.wait(), 10)
        with pytest.raises(DatabaseError):
            await sync_to_async(_lock_row_is_free)()
    finally:
        release.set()
        await asyncio.wait_for(holder, 10)

    assert await sync_to_async(_lock_row_is_free)()
