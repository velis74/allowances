"""
Scenarios for the Django adapter in its async-first form.

The async shortcuts and ``allowances_django.store.DjangoStore`` run on the event loop; the synchronous shortcuts in
``allowances_django.sync.shortcuts`` and ``allowances_django.sync.store.DjangoStore`` run from plain synchronous
code. ``AllowancesBackend`` answers ``has_perm`` through the synchronous API and ``ahas_perm`` through the async one.
``allowances_adorm.store.AdormStore`` serves the async shortcuts when ``ALLOWANCES_STORE`` names it.

Every test commits (``transaction=True``): the async store runs a unit of work on its own thread with its own
connection, so a test transaction held by the main connection would hide the data from it.
"""

import asyncio
import inspect

from datetime import timedelta
from decimal import Decimal

import pytest

from asgiref.sync import sync_to_async
from django.contrib.auth.models import Group, User
from django.db import connection, connections
from django.test import override_settings
from django.utils import timezone

import allowances
import allowances.sync as sync

from allowances.errors import InsufficientBalanceError, StoreSuspendedError
from allowances.types import ObjectRef, SubjectRef, WalletSpec, WalletType
from allowances_django import permissions, shortcuts
from allowances_django.models import Transaction, Wallet as WalletRow
from allowances_django.providers import SettingsProvider
from allowances_django.store import DjangoStore
from allowances_django.subjects import subject_ref
from allowances_django.sync import permissions as sync_permissions, shortcuts as sync_shortcuts
from allowances_django.sync.store import DjangoStore as SyncDjangoStore

pytestmark = pytest.mark.django_db(transaction=True)

PERM = "news.change_article"
BACKEND = "allowances_django.permissions.AllowancesBackend"

CONFIG = {
    "currencies": {"eur": {}, "edit_right": {}},
    "features": {
        "pay": {
            "consumable": {"currencies": ["eur"]},
            "subjects": {"user"},
            "fulfillment_order": [("consumable", "user")],
        },
        "edit_article": {
            "grant": {"currencies": [("edit_right", 0)]},
            "subjects": {"user"},
            "fulfillment_order": [("grant", "user")],
        },
    },
}

needs_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="row locks under concurrent transactions need PostgreSQL"
)


@pytest.fixture
def provider():
    return SettingsProvider(config=CONFIG)


@pytest.fixture
def alice():
    return User.objects.create(username="alice")


@pytest.fixture
def bob():
    return User.objects.create(username="bob")


@pytest.fixture
def editors():
    return Group.objects.create(name="editors")


@pytest.fixture
def staff():
    return Group.objects.create(name="staff")


@pytest.fixture
def allowances_backend():
    with override_settings(AUTHENTICATION_BACKENDS=[BACKEND]):
        yield


def money(amount):
    # overdraft_limit=0 keeps a wallet drained to zero out of the archive, so its balance stays readable.
    return WalletSpec(WalletType.CONSUMABLE, "eur", Decimal(str(amount)), overdraft_limit=Decimal("0"))


def right():
    return WalletSpec(WalletType.GRANT, "edit_right", Decimal("1"))


async def balance_of(wallet_id):
    return (await WalletRow.objects.aget(pk=wallet_id)).balance


def sync_balance_of(wallet_id):
    return WalletRow.objects.get(pk=wallet_id).balance


# -- async shortcuts ------------------------------------------------------------------------------


def test_async_shortcuts_and_permissions_are_coroutine_functions():
    for name in (
        "peek",
        "fulfill",
        "attach",
        "unfulfill",
        "plan_switch",
        "cancel_scheduled_switch",
        "open_wallet",
        "credit",
        "transfer",
        "adjust_wallet",
        "close_wallet",
        "consumed",
        "archive_expired_wallets",
        "find_unattached_fulfillment_records",
        "add_member",
        "remove_member",
        "groups_of",
        "effective_allowances",
    ):
        assert inspect.iscoroutinefunction(getattr(shortcuts, name)), name
        assert not inspect.iscoroutinefunction(getattr(sync_shortcuts, name)), name
        assert not hasattr(shortcuts, "a" + name), name
    for name in ("grant_permission", "revoke_permission"):
        assert inspect.iscoroutinefunction(getattr(permissions, name)), name
        assert not inspect.iscoroutinefunction(getattr(sync_permissions, name)), name


def test_pure_permission_helpers_stay_plain_and_are_shared_with_sync():
    assert not inspect.iscoroutinefunction(permissions.effective_permissions)
    assert not inspect.iscoroutinefunction(permissions.permission_feature)
    assert sync_permissions.effective_permissions is permissions.effective_permissions
    assert sync_permissions.permission_feature is permissions.permission_feature
    assert sync_permissions.permission_feature(PERM) == "perm:news.change_article"


async def test_async_fulfil_attach_and_unfulfil_with_model_instances(alice, editors, provider):
    wallet = await shortcuts.open_wallet(alice, money(10), source="plan")

    peeked = await shortcuts.peek("pay", 4, {"user": alice}, provider=provider)
    assert peeked.satisfied is True
    assert await balance_of(wallet.id) == Decimal("10")

    result = await shortcuts.fulfill("pay", 4, {"user": alice}, actor=alice, provider=provider)

    assert result.satisfied is True
    assert await balance_of(wallet.id) == Decimal("6")
    assert [r.guid for r in await shortcuts.find_unattached_fulfillment_records()] == [result.guid]

    await shortcuts.attach(result.guid, editors)
    assert await shortcuts.find_unattached_fulfillment_records() == []
    attached = await Transaction.objects.filter(wallet_id=wallet.id).afirst()
    assert (attached.object_type, attached.object_id) == ("auth.group", str(editors.pk))
    assert (attached.actor_type, attached.actor_id) == ("auth.user", str(alice.pk))

    await shortcuts.unfulfill(result.guid)
    await shortcuts.unfulfill(result.guid)  # idempotent

    assert await balance_of(wallet.id) == Decimal("10")


async def test_async_refused_fulfil_writes_nothing(alice, provider):
    wallet = await shortcuts.open_wallet(alice, money(3), source="plan")

    with pytest.raises(InsufficientBalanceError) as raised:
        await shortcuts.fulfill("pay", 5, {"user": alice}, provider=provider)

    assert raised.value.result.granted == Decimal("3")
    assert await balance_of(wallet.id) == Decimal("3")
    assert await Transaction.objects.filter(wallet_id=wallet.id).acount() == 0


async def test_async_memberships_return_model_instances_and_confer_rights(alice, editors, staff, provider):
    await shortcuts.open_wallet(staff, right(), source="grant")
    await shortcuts.add_member(alice, editors)
    await shortcuts.add_member(editors, staff)

    assert await shortcuts.groups_of(alice) == [editors, staff]
    assert (await shortcuts.peek("edit_article", 1, {"user": alice}, provider=provider)).satisfied is True

    held = await shortcuts.effective_allowances(alice)
    assert [(a.holder, a.depth) for a in held] == [(subject_ref(staff), 2)]

    assert await shortcuts.remove_member(editors, staff) is True
    assert await shortcuts.remove_member(editors, staff) is False
    assert await shortcuts.groups_of(alice) == [editors]
    assert (await shortcuts.peek("edit_article", 1, {"user": alice}, provider=provider)).satisfied is False


async def test_async_groups_of_leaves_out_a_deleted_group(alice, editors):
    await shortcuts.add_member(alice, editors)
    await shortcuts.add_member(alice, SubjectRef("auth.group", "999999"))

    assert await shortcuts.groups_of(alice) == [editors]


async def test_async_credit_transfer_and_reversal(alice, bob):
    source = await shortcuts.open_wallet(alice, money(100), source="wallet")
    target = await shortcuts.open_wallet(bob, money(0), source="wallet")

    await shortcuts.credit(source, 20, actor=alice)
    result = await shortcuts.transfer(source.id, target.id, 30, actor=alice)

    assert result.satisfied is True
    assert await balance_of(source.id) == Decimal("90")
    assert await balance_of(target.id) == Decimal("30")

    await shortcuts.unfulfill(result.guid)

    assert await balance_of(source.id) == Decimal("120")
    assert await balance_of(target.id) == Decimal("0")


async def test_async_plan_switch_scheduled_and_cancelled(alice):
    [basic] = await shortcuts.plan_switch({"user": alice}, [money(10)])
    switch_at = timezone.now() + timedelta(days=30)

    await shortcuts.plan_switch({"user": alice}, [money(50)], switch_at)
    later = switch_at + timedelta(seconds=1)
    assert [a.balance for a in await shortcuts.effective_allowances(alice, at=later)] == [Decimal("50")]

    assert await shortcuts.cancel_scheduled_switch({"user": alice}) == 1
    assert [a.wallet.id for a in await shortcuts.effective_allowances(alice, at=later)] == [basic.id]


async def test_async_close_and_archive(alice):
    wallet = await shortcuts.open_wallet(alice, money(10), source="plan")
    now = timezone.now()

    await shortcuts.close_wallet(wallet, at=now)

    assert await shortcuts.archive_expired_wallets(now) == 1
    assert await shortcuts.archive_expired_wallets(now) == 0
    assert not await WalletRow.objects.filter(pk=wallet.id).aexists()


# -- async store ----------------------------------------------------------------------------------


async def test_async_store_serves_the_core_api(alice, provider):
    store = DjangoStore()
    subject = subject_ref(alice)
    wallet = await allowances.open_wallet(store, subject, money(10), source="plan")

    result = await allowances.fulfill(store, "pay", 4, {"user": subject}, provider)

    assert result.satisfied is True
    assert (await store.get_wallet(wallet.id)).balance == Decimal("6")
    assert await balance_of(wallet.id) == Decimal("6")


async def test_async_store_unit_of_work_that_raises_discards_its_writes(alice, provider):
    store = DjangoStore()
    subject = subject_ref(alice)
    wallet = await allowances.open_wallet(store, subject, money(10), source="plan")

    with pytest.raises(RuntimeError):
        async with store.unit_of_work():
            await allowances.fulfill(store, "pay", 4, {"user": subject}, provider)
            raise RuntimeError("operation failed")

    assert await balance_of(wallet.id) == Decimal("10")
    assert await Transaction.objects.filter(wallet_id=wallet.id).acount() == 0


async def test_async_store_nested_refusal_keeps_the_outer_writes(alice, provider):
    store = DjangoStore()
    subject = subject_ref(alice)
    wallet = await allowances.open_wallet(store, subject, money(10), source="plan")

    async with store.unit_of_work():
        await allowances.fulfill(store, "pay", 4, {"user": subject}, provider)
        with pytest.raises(InsufficientBalanceError):
            await allowances.fulfill(store, "pay", 100, {"user": subject}, provider)
        async with store.unit_of_work():
            await allowances.credit(store, wallet.id, 1)

    assert await balance_of(wallet.id) == Decimal("7")


@needs_postgresql
async def test_concurrent_units_of_work_do_not_share_a_transaction(alice, bob, provider):
    """Two units of work open at once: the one that raises rolls back alone, the other commits alone."""
    store = DjangoStore()
    alice_ref, bob_ref = subject_ref(alice), subject_ref(bob)
    alice_wallet = await allowances.open_wallet(store, alice_ref, money(10), source="plan")
    bob_wallet = await allowances.open_wallet(store, bob_ref, money(10), source="plan")
    alice_wrote, bob_wrote = asyncio.Event(), asyncio.Event()

    async def commits():
        async with store.unit_of_work():
            await allowances.fulfill(store, "pay", 2, {"user": alice_ref}, provider)
            alice_wrote.set()
            await asyncio.wait_for(bob_wrote.wait(), 30)

    async def rolls_back():
        await asyncio.wait_for(alice_wrote.wait(), 30)
        async with store.unit_of_work():
            await allowances.fulfill(store, "pay", 3, {"user": bob_ref}, provider)
            bob_wrote.set()
            raise RuntimeError("operation failed")

    outcomes = await asyncio.gather(commits(), rolls_back(), return_exceptions=True)

    assert outcomes[0] is None
    assert isinstance(outcomes[1], RuntimeError)
    assert await balance_of(alice_wallet.id) == Decimal("8")
    assert await balance_of(bob_wallet.id) == Decimal("10")


# -- store selection by settings ------------------------------------------------------------------


class CountingStore(DjangoStore):
    """An async DjangoStore that counts its instances."""

    created = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        type(self).created += 1


class CountingSyncStore(SyncDjangoStore):
    """A sync DjangoStore that counts its instances."""

    created = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        type(self).created += 1


def refusing_factory():
    raise AssertionError("the configured store must not be used when store= is given")


async def test_async_shortcuts_use_the_store_named_in_settings(alice):
    CountingStore.created = 0
    with override_settings(ALLOWANCES_STORE="tests.django.test_scenarios_async_django.CountingStore"):
        wallet = await shortcuts.open_wallet(alice, money(10), source="plan")

    assert CountingStore.created >= 1
    assert await balance_of(wallet.id) == Decimal("10")


async def test_async_shortcuts_accept_a_store_factory_in_settings(alice):
    made = []

    def factory():
        made.append(DjangoStore())
        return made[-1]

    with override_settings(ALLOWANCES_STORE=factory):
        await shortcuts.open_wallet(alice, money(10), source="plan")

    assert made


async def test_an_explicit_store_wins_over_the_async_setting(alice):
    with override_settings(ALLOWANCES_STORE=refusing_factory):
        wallet = await shortcuts.open_wallet(alice, money(10), source="plan", store=DjangoStore())

    assert await balance_of(wallet.id) == Decimal("10")


def test_sync_shortcuts_use_the_store_named_in_settings(alice):
    CountingSyncStore.created = 0
    with override_settings(ALLOWANCES_SYNC_STORE="tests.django.test_scenarios_async_django.CountingSyncStore"):
        wallet = sync_shortcuts.open_wallet(alice, money(10), source="plan")

    assert CountingSyncStore.created >= 1
    assert sync_balance_of(wallet.id) == Decimal("10")


def test_an_explicit_store_wins_over_the_sync_setting(alice):
    with override_settings(ALLOWANCES_SYNC_STORE=refusing_factory):
        wallet = sync_shortcuts.open_wallet(alice, money(10), source="plan", store=SyncDjangoStore())

    assert sync_balance_of(wallet.id) == Decimal("10")


# -- sync shortcuts and sync store ----------------------------------------------------------------


def test_sync_fulfil_attach_and_unfulfil_with_model_instances(alice, editors, provider):
    wallet = sync_shortcuts.open_wallet(alice, money(10), source="plan")

    assert sync_shortcuts.peek("pay", 4, {"user": alice}, provider=provider).satisfied is True
    result = sync_shortcuts.fulfill("pay", 4, {"user": alice}, provider=provider)

    assert sync_balance_of(wallet.id) == Decimal("6")
    assert [r.guid for r in sync_shortcuts.find_unattached_fulfillment_records()] == [result.guid]

    sync_shortcuts.attach(result.guid, editors)
    assert sync_shortcuts.find_unattached_fulfillment_records() == []

    sync_shortcuts.unfulfill(result.guid)
    assert sync_balance_of(wallet.id) == Decimal("10")

    with pytest.raises(InsufficientBalanceError):
        sync_shortcuts.fulfill("pay", 11, {"user": alice}, provider=provider)
    assert sync_balance_of(wallet.id) == Decimal("10")


def test_sync_memberships_return_model_instances(alice, editors, staff, provider):
    sync_shortcuts.open_wallet(staff, right(), source="grant")
    sync_shortcuts.add_member(alice, editors)
    sync_shortcuts.add_member(editors, staff)

    assert sync_shortcuts.groups_of(alice) == [editors, staff]
    held = sync_shortcuts.effective_allowances(alice)
    assert [(a.holder, a.depth) for a in held] == [(subject_ref(staff), 2)]
    assert sync_shortcuts.peek("edit_article", 1, {"user": alice}, provider=provider).satisfied is True

    assert sync_shortcuts.remove_member(alice, editors) is True
    assert sync_shortcuts.groups_of(alice) == []
    assert sync_shortcuts.peek("edit_article", 1, {"user": alice}, provider=provider).satisfied is False


def test_sync_credit_transfer_plan_switch_and_archive(alice, bob):
    source = sync_shortcuts.open_wallet(alice, money(100), source="wallet")
    target = sync_shortcuts.open_wallet(bob, money(0), source="wallet")

    sync_shortcuts.credit(source.id, 20)
    result = sync_shortcuts.transfer(source, target, 30)
    assert (sync_balance_of(source.id), sync_balance_of(target.id)) == (Decimal("90"), Decimal("30"))
    sync_shortcuts.unfulfill(result.guid)
    assert (sync_balance_of(source.id), sync_balance_of(target.id)) == (Decimal("120"), Decimal("0"))

    [basic] = sync_shortcuts.plan_switch({"user": alice}, [money(10)])
    now = timezone.now()
    sync_shortcuts.close_wallet(basic, at=now)
    assert sync_shortcuts.archive_expired_wallets(now) == 1
    assert not WalletRow.objects.filter(pk=basic.id).exists()


def test_sync_store_serves_allowances_sync_and_rolls_back(alice, provider):
    store = SyncDjangoStore()
    subject = subject_ref(alice)
    wallet = sync.open_wallet(store, subject, money(10), source="plan")

    result = sync.fulfill(store, "pay", 4, {"user": subject}, provider)
    assert result.satisfied is True
    assert sync_balance_of(wallet.id) == Decimal("6")

    sync.unfulfill(store, result.guid)
    assert sync_balance_of(wallet.id) == Decimal("10")

    with pytest.raises(InsufficientBalanceError):
        sync.fulfill(store, "pay", 11, {"user": subject}, provider)
    assert sync_balance_of(wallet.id) == Decimal("10")


def test_allowances_sync_refuses_the_async_store(alice, provider):
    """The async store's unit of work hands work to a thread and suspends, which the sync driver refuses."""
    store = DjangoStore()
    subject = subject_ref(alice)
    wallet = sync.open_wallet(SyncDjangoStore(), subject, money(10), source="plan")

    with pytest.raises(StoreSuspendedError):
        sync.fulfill(store, "pay", 4, {"user": subject}, provider)

    assert sync_balance_of(wallet.id) == Decimal("10")


async def test_sync_shortcuts_run_in_a_worker_thread_next_to_async_code(alice, provider):
    wallet = await asyncio.to_thread(sync_shortcuts.open_wallet, alice, money(10), source="plan")

    await asyncio.to_thread(sync_shortcuts.fulfill, "pay", 4, {"user": alice}, provider=provider)

    assert await balance_of(wallet.id) == Decimal("6")


# -- permissions ----------------------------------------------------------------------------------


async def test_async_grant_and_revoke_permission(alice):
    wallet = await permissions.grant_permission(alice, PERM)

    assert wallet.currency == "perm:news.change_article"
    assert wallet.wallet_type == "grant"
    assert wallet.balance == 1
    assert wallet.source == "permission"
    held = permissions.effective_permissions(await shortcuts.effective_allowances(alice))
    assert list(held) == [PERM]

    assert await permissions.revoke_permission(alice, PERM) == 1
    assert await permissions.revoke_permission(alice, PERM) == 0


def test_sync_grant_and_revoke_permission(alice):
    wallet = sync_permissions.grant_permission(alice, PERM, valid_until=timezone.now() + timedelta(hours=1))

    assert wallet.currency == "perm:news.change_article"
    held = sync_permissions.effective_permissions(sync_shortcuts.effective_allowances(alice))
    assert list(held) == [PERM]

    assert sync_permissions.revoke_permission(alice, PERM) == 1
    assert sync_permissions.effective_permissions(sync_shortcuts.effective_allowances(alice)) == {}


@pytest.mark.usefixtures("allowances_backend")
def test_has_perm_sees_a_direct_grant_and_a_group_grant(alice, bob, editors):
    sync_permissions.grant_permission(alice, PERM)
    sync_permissions.grant_permission(editors, "news.add_article")
    sync_shortcuts.add_member(bob, editors)

    alice = User.objects.get(pk=alice.pk)
    bob = User.objects.get(pk=bob.pk)
    assert alice.has_perm(PERM) is True
    assert alice.has_perm("news.add_article") is False
    assert bob.has_perm(PERM) is False
    assert bob.has_perm("news.add_article") is True
    assert bob.get_all_permissions() == {"news.add_article"}
    assert bob.has_module_perms("news") is True


@pytest.mark.usefixtures("allowances_backend")
async def test_ahas_perm_sees_a_direct_grant_and_a_group_grant(alice, bob, editors):
    await permissions.grant_permission(alice, PERM)
    await permissions.grant_permission(editors, "news.add_article")
    await shortcuts.add_member(bob, editors)

    alice = await User.objects.aget(pk=alice.pk)
    bob = await User.objects.aget(pk=bob.pk)
    assert await alice.ahas_perm(PERM) is True
    assert await alice.ahas_perm("news.add_article") is False
    assert await bob.ahas_perm(PERM) is False
    assert await bob.ahas_perm("news.add_article") is True
    assert await bob.aget_all_permissions() == {"news.add_article"}
    assert await bob.ahas_module_perms("news") is True
    assert await bob.ahas_module_perms("club") is False


@pytest.mark.usefixtures("allowances_backend")
async def test_has_perm_and_ahas_perm_agree_and_follow_revocation(alice, editors):
    await permissions.grant_permission(editors, PERM)
    await shortcuts.add_member(alice, editors)

    user = await User.objects.aget(pk=alice.pk)
    assert await sync_to_async(user.has_perm)(PERM) is True
    assert await (await User.objects.aget(pk=alice.pk)).ahas_perm(PERM) is True

    await shortcuts.remove_member(alice, editors)

    assert await (await User.objects.aget(pk=alice.pk)).ahas_perm(PERM) is False
    assert await sync_to_async((await User.objects.aget(pk=alice.pk)).has_perm)(PERM) is False


@pytest.mark.usefixtures("allowances_backend")
async def test_has_perm_and_ahas_perm_share_the_per_user_cache(alice):
    await permissions.grant_permission(alice, PERM)
    user = await User.objects.aget(pk=alice.pk)

    assert await sync_to_async(user.has_perm)(PERM) is True
    await permissions.revoke_permission(alice, PERM)

    # The answer cached on the user object by has_perm is the one ahas_perm returns.
    assert await user.ahas_perm(PERM) is True
    assert await (await User.objects.aget(pk=alice.pk)).ahas_perm(PERM) is False


@pytest.mark.usefixtures("allowances_backend")
async def test_ahas_perm_denies_an_inactive_user(alice):
    await permissions.grant_permission(alice, PERM)
    alice.is_active = False
    await alice.asave()

    assert await (await User.objects.aget(pk=alice.pk)).ahas_perm(PERM) is False


# -- concurrency ----------------------------------------------------------------------------------


WORKERS = 8


@needs_postgresql
async def test_concurrent_async_fulfils_on_one_wallet_never_overdraw(alice, provider):
    """Eight concurrent fulfils of two from a wallet of ten: five win, three are refused, the balance is zero."""
    wallet = await shortcuts.open_wallet(alice, money(10), source="plan")

    async def draw():
        try:
            return await shortcuts.fulfill("pay", 2, {"user": alice}, provider=provider)
        except InsufficientBalanceError:
            return None

    outcomes = await asyncio.wait_for(asyncio.gather(*(draw() for _ in range(WORKERS)), return_exceptions=True), 60)

    unexpected = [o for o in outcomes if isinstance(o, BaseException)]
    assert not unexpected, f"concurrent fulfilment raised: {unexpected}"
    granted = [o for o in outcomes if o is not None]
    assert len(granted) == 5
    assert await balance_of(wallet.id) == Decimal("0")
    amounts = [t.amount async for t in Transaction.objects.filter(wallet_id=wallet.id)]
    assert len(amounts) == 5
    assert sum(amounts) == Decimal("-10")


@needs_postgresql
async def test_concurrent_async_fulfils_across_two_wallets_never_double_draw(alice, provider):
    """Requests spanning two wallets, each fulfil on its own store instance."""
    subject = subject_ref(alice)
    first = await allowances.open_wallet(DjangoStore(), subject, money(5), source="plan")
    second = await allowances.open_wallet(DjangoStore(), subject, money(7), source="plan")

    async def draw():
        try:
            return await allowances.fulfill(DjangoStore(), "pay", 3, {"user": subject}, provider)
        except InsufficientBalanceError:
            return None

    outcomes = await asyncio.wait_for(asyncio.gather(*(draw() for _ in range(WORKERS)), return_exceptions=True), 60)

    unexpected = [o for o in outcomes if isinstance(o, BaseException)]
    assert not unexpected, f"concurrent fulfilment raised: {unexpected}"
    granted = [o for o in outcomes if o is not None]
    assert len(granted) == 4
    balances = [await balance_of(first.id), await balance_of(second.id)]
    assert balances == [Decimal("0"), Decimal("0")]
    drawn = -sum([t.amount async for t in Transaction.objects.filter(wallet_id__in=[first.id, second.id])])
    assert drawn == Decimal("12") == sum(o.granted for o in granted)


@needs_postgresql
async def test_concurrent_async_transfers_and_fulfils_keep_the_ledger_consistent(alice, bob, provider):
    source = await shortcuts.open_wallet(alice, money(10), source="wallet")
    target = await shortcuts.open_wallet(bob, money(0), source="wallet")

    async def move():
        try:
            return await shortcuts.transfer(source.id, target.id, 2)
        except InsufficientBalanceError:
            return None

    async def spend():
        try:
            return await shortcuts.fulfill("pay", 2, {"user": alice}, provider=provider)
        except InsufficientBalanceError:
            return None

    work = [move() if i % 2 else spend() for i in range(WORKERS)]
    outcomes = await asyncio.wait_for(asyncio.gather(*work, return_exceptions=True), 60)

    unexpected = [o for o in outcomes if isinstance(o, BaseException)]
    assert not unexpected, f"concurrent work raised: {unexpected}"
    assert len([o for o in outcomes if o is not None]) == 5
    assert await balance_of(source.id) == Decimal("0")
    moved = sum([t.amount async for t in Transaction.objects.filter(wallet_id=target.id)])
    assert await balance_of(target.id) == moved


# -- object refs ----------------------------------------------------------------------------------


async def test_async_attach_accepts_an_object_ref(alice, provider):
    await shortcuts.open_wallet(alice, money(10), source="plan")
    result = await shortcuts.fulfill("pay", 1, {"user": alice}, provider=provider)

    await shortcuts.attach(result.guid, ObjectRef("shop.order", "17"))

    assert await shortcuts.find_unattached_fulfillment_records() == []


# -- adorm store ----------------------------------------------------------------------------------


@pytest.fixture(params=["adorm-django", "adorm-asyncpg"])
async def adorm_session(request):
    """
    An adorm session of the kind's profile; on exit, the event loop's adorm pools and the connection of Django's
    asynchronous ORM are closed.
    """
    from adorm import create_session
    from adorm.settings import close_pools

    from tests.adorm_support import configure_adorm, PROFILES

    if request.param == "adorm-asyncpg" and connection.vendor != "postgresql":
        pytest.skip("adorm's native driver (asyncpg) runs on PostgreSQL only")
    configure_adorm()
    yield create_session(PROFILES[request.param], "scenarios")
    await close_pools()
    await sync_to_async(connections.close_all)()


ADORM_STORE = override_settings(ALLOWANCES_STORE="allowances_adorm.store.AdormStore")


async def test_async_shortcuts_run_on_the_adorm_store_named_in_settings(adorm_session, alice, editors, provider):
    with adorm_session.activate(), ADORM_STORE:
        wallet = await shortcuts.open_wallet(alice, money(10), source="plan")
        result = await shortcuts.fulfill("pay", 4, {"user": alice}, actor=alice, provider=provider)
        await shortcuts.attach(result.guid, editors)

        assert await balance_of(wallet.id) == Decimal("6")
        assert await shortcuts.find_unattached_fulfillment_records() == []
        attached = await Transaction.objects.filter(wallet_id=wallet.id).afirst()
        assert (attached.object_type, attached.object_id) == ("auth.group", str(editors.pk))

        await shortcuts.unfulfill(result.guid)

        assert await balance_of(wallet.id) == Decimal("10")


async def test_a_callers_adorm_atomic_block_rolls_back_the_adorm_stores_writes(adorm_session, alice, provider):
    import adorm

    with adorm_session.activate(), ADORM_STORE:
        wallet = await shortcuts.open_wallet(alice, money(10), source="plan")

        with pytest.raises(RuntimeError):
            async with adorm.atomic():
                await shortcuts.fulfill("pay", 4, {"user": alice}, provider=provider)
                raise RuntimeError("operation failed")

    assert await balance_of(wallet.id) == Decimal("10")
    assert await Transaction.objects.filter(wallet_id=wallet.id).acount() == 0


@pytest.mark.parametrize("kind", ["adorm-django", "adorm-asyncpg"])
@pytest.mark.django_db(transaction=True)
async def test_groups_of_and_revoke_permission_work_with_the_adorm_store(kind):
    from tests.conftest import store_maker

    async with store_maker(kind) as factory:
        store = factory()
        alice = await User.objects.acreate(username="alice")
        editors = await Group.objects.acreate(name="editors")
        await shortcuts.add_member(alice, editors, store=store)
        await permissions.grant_permission(alice, PERM, store=store)

        assert await shortcuts.groups_of(alice, store=store) == [editors]
        assert await permissions.revoke_permission(alice, PERM, store=store) == 1
        assert await permissions.revoke_permission(alice, PERM, store=store) == 0
