"""
Scenarios for the async-first core API and its synchronous twin ``allowances.sync``.

The async API is awaited on an event loop, once per store (``store`` in ``tests/conftest.py``). ``allowances.sync``
drives the same functions without an event loop on ``MemoryStore`` and refuses a store whose methods suspend.
"""

import asyncio
import inspect

from datetime import timedelta
from decimal import Decimal

import pytest

import allowances
import allowances.sync as sync

from allowances.backends import MemoryStore
from allowances.definitions import DictProvider
from allowances.errors import (
    AllowancesError,
    InsufficientBalanceError,
    MembershipCycleError,
    StoreSuspendedError,
)
from allowances.types import ObjectRef, SubjectRef, TransactionKind, WalletSpec, WalletType
from allowances.windows import utcnow
from tests.api import (
    add_member,
    archive_expired_wallets,
    attach,
    cancel_scheduled_switch,
    close_wallet,
    consumed,
    credit,
    effective_allowances,
    find_unattached_fulfillment_records,
    fulfill,
    groups_of,
    open_wallet,
    peek,
    plan_switch,
    remove_member,
    transfer,
    unfulfill,
)

ALICE = SubjectRef("auth.user", 1)
BOB = SubjectRef("auth.user", 2)
EDITORS = SubjectRef("auth.group", 10)
STAFF = SubjectRef("auth.group", 20)

CONFIG = {
    "currencies": {"edit_right": {}, "eur": {}},
    "features": {
        "edit_article": {
            "grant": {"currencies": [("edit_right", 0)]},
            "subjects": {"user"},
            "fulfillment_order": [("grant", "user")],
        },
        "pay": {
            "consumable": {"currencies": ["eur"]},
            "subjects": {"user"},
            "fulfillment_order": [("consumable", "user")],
        },
    },
}


@pytest.fixture
def memory_store():
    return MemoryStore()


@pytest.fixture
def provider():
    return DictProvider(CONFIG)


def right():
    return WalletSpec(WalletType.GRANT, "edit_right", Decimal("1"))


def money(amount):
    # overdraft_limit=0 keeps a wallet drained to zero out of the archive, so its balance stays readable.
    return WalletSpec(WalletType.CONSUMABLE, "eur", Decimal(str(amount)), overdraft_limit=Decimal("0"))


def balance_in(allowances_list, wallet_id):
    [match] = [a for a in allowances_list if a.wallet.id == wallet_id]
    return match.balance


async def balance(store, wallet):
    return balance_in(await effective_allowances(store, wallet.subject), wallet.id)


def sync_balance(store, wallet):
    return balance_in(sync.effective_allowances(store, wallet.subject), wallet.id)


# -- the async API is coroutines ------------------------------------------------------------------


@pytest.mark.parametrize(
    "function",
    [
        allowances.peek,
        allowances.fulfill,
        allowances.attach,
        allowances.unfulfill,
        allowances.credit,
        allowances.transfer,
        allowances.open_wallet,
        allowances.close_wallet,
        allowances.consumed,
        allowances.plan_switch,
        allowances.cancel_scheduled_switch,
        allowances.archive_expired_wallets,
        allowances.find_unattached_fulfillment_records,
        allowances.effective_allowances,
        allowances.add_member,
        allowances.remove_member,
        allowances.groups_of,
        allowances.adjust_wallet,
        allowances.effective_balance,
    ],
)
def test_every_engine_function_is_a_coroutine_function_with_a_plain_name(function):
    assert inspect.iscoroutinefunction(function)
    assert not hasattr(allowances, "a" + function.__name__)
    assert not hasattr(sync, "a" + function.__name__)


@pytest.mark.parametrize(
    "name",
    [
        "unit_of_work",
        "lock_wallets",
        "find_wallets",
        "get_wallet",
        "save_wallet",
        "create_wallet",
        "find_scheduled_wallets",
        "find_expired_wallets",
        "archive_wallet",
        "sum_transactions",
        "transactions_for_wallet",
        "create_transaction",
        "transactions_for_record",
        "attach_object",
        "get_or_create_record",
        "get_record",
        "records_with_unattached_transactions",
        "add_membership",
        "remove_membership",
        "memberships_of",
        "lock_memberships",
        "find_modifier_factors",
    ],
)
def test_every_memory_store_method_is_async(name):
    method = getattr(MemoryStore(), name)
    if name == "unit_of_work":
        manager = method()
        assert hasattr(manager, "__aenter__") and hasattr(manager, "__aexit__")
    else:
        assert inspect.iscoroutinefunction(method)


def test_pure_helpers_stay_plain_functions():
    assert not inspect.iscoroutinefunction(allowances.windows.window_start)
    assert not inspect.iscoroutinefunction(DictProvider(CONFIG).get_feature)


# -- rights ---------------------------------------------------------------------------------------


async def test_a_held_right_is_peeked_and_used_without_being_consumed(store, provider):
    wallet = await open_wallet(store, ALICE, right(), source="grant")

    assert (await peek(store, "edit_article", 5, {"user": ALICE}, provider)).satisfied is True

    result = await fulfill(store, "edit_article", 1, {"user": ALICE}, provider)

    assert result.satisfied is True
    assert [(t.kind, t.amount, t.actor) for t in result.transactions] == [
        (TransactionKind.CONSUMPTION, Decimal("0"), ALICE)
    ]
    assert await balance(store, wallet) == Decimal("1")


async def test_without_a_right_peek_is_unsatisfied_and_fulfill_raises(store, provider):
    assert (await peek(store, "edit_article", 1, {"user": ALICE}, provider)).satisfied is False

    with pytest.raises(InsufficientBalanceError) as raised:
        await fulfill(store, "edit_article", 1, {"user": ALICE}, provider)

    assert raised.value.result.satisfied is False


async def test_a_closed_right_is_no_longer_held(store, provider):
    wallet = await open_wallet(store, ALICE, right(), source="grant")
    now = utcnow()

    closed = await close_wallet(store, wallet.id, at=now)

    assert closed.valid_until == now
    later = now + timedelta(seconds=1)
    assert (await peek(store, "edit_article", 1, {"user": ALICE}, provider, at=later)).satisfied is False


# -- memberships ----------------------------------------------------------------------------------


async def test_a_member_inherits_a_right_through_nested_groups_and_loses_it_on_leaving(store, provider):
    await open_wallet(store, STAFF, right(), source="grant")
    await add_member(store, ALICE, EDITORS)
    await add_member(store, EDITORS, STAFF)

    assert await groups_of(store, ALICE) == [EDITORS, STAFF]
    assert (await peek(store, "edit_article", 1, {"user": ALICE}, provider)).satisfied is True

    assert await remove_member(store, EDITORS, STAFF) is True
    assert await remove_member(store, EDITORS, STAFF) is False
    assert await groups_of(store, ALICE) == [EDITORS]
    assert (await peek(store, "edit_article", 1, {"user": ALICE}, provider)).satisfied is False


async def test_a_membership_cycle_is_rejected(store):
    await add_member(store, ALICE, EDITORS)
    await add_member(store, EDITORS, STAFF)

    with pytest.raises(MembershipCycleError):
        await add_member(store, STAFF, ALICE)

    assert await groups_of(store, STAFF) == []


async def test_effective_allowances_lists_own_and_inherited_wallets_by_depth(store):
    own = await open_wallet(store, ALICE, money(5), source="plan")
    inherited = await open_wallet(store, EDITORS, right(), source="grant")
    await add_member(store, ALICE, EDITORS)

    result = await effective_allowances(store, ALICE)

    assert [(a.wallet.id, a.holder, a.depth, a.balance) for a in result] == [
        (own.id, ALICE, 0, Decimal("5")),
        (inherited.id, EDITORS, 1, Decimal("1")),
    ]
    grants = await effective_allowances(store, ALICE, wallet_types=[WalletType.GRANT])
    assert [a.wallet.id for a in grants] == [inherited.id]
    eur = await effective_allowances(store, ALICE, currencies=["eur"])
    assert [a.wallet.id for a in eur] == [own.id]


# -- peek, fulfill, attach, unfulfill -------------------------------------------------------------


async def test_peek_reports_without_writing(store, provider):
    wallet = await open_wallet(store, ALICE, money(10), source="plan")

    result = await peek(store, "pay", 4, {"user": ALICE}, provider)

    assert result.satisfied is True
    assert result.granted == Decimal("4")
    assert await balance(store, wallet) == Decimal("10")
    assert await find_unattached_fulfillment_records(store) == []


async def test_fulfill_attach_and_unfulfill_round_trip(store, provider):
    wallet = await open_wallet(store, ALICE, money(10), source="plan")
    start = utcnow() - timedelta(minutes=1)

    result = await fulfill(store, "pay", 4, {"user": ALICE}, provider)

    assert result.satisfied is True
    assert await balance(store, wallet) == Decimal("6")
    assert [r.guid for r in await find_unattached_fulfillment_records(store)] == [result.guid]

    await attach(store, result.guid, ObjectRef("shop.order", 1))
    assert await find_unattached_fulfillment_records(store) == []

    await unfulfill(store, result.guid, actor=BOB)
    await unfulfill(store, result.guid)  # idempotent

    assert await balance(store, wallet) == Decimal("10")
    assert await consumed(store, wallet.id, start, utcnow() + timedelta(minutes=1)) == Decimal("0")


async def test_a_refused_fulfill_writes_nothing_and_carries_the_result(store, provider):
    wallet = await open_wallet(store, ALICE, money(3), source="plan")

    with pytest.raises(InsufficientBalanceError) as raised:
        await fulfill(store, "pay", 5, {"user": ALICE}, provider)

    assert raised.value.result.granted == Decimal("3")
    assert await balance(store, wallet) == Decimal("3")


async def test_allow_partial_takes_what_is_there(store, provider):
    wallet = await open_wallet(store, ALICE, money(3), source="plan")

    result = await fulfill(store, "pay", 5, {"user": ALICE}, provider, allow_partial=True)

    assert result.satisfied is False
    assert result.granted == Decimal("3")
    assert await balance(store, wallet) == Decimal("0")


async def test_two_fulfils_under_one_guid_share_one_record(store, provider):
    wallet = await open_wallet(store, ALICE, money(10), source="plan")

    first = await fulfill(store, "pay", 2, {"user": ALICE}, provider)
    second = await fulfill(store, "pay", 3, {"user": ALICE}, provider, guid=first.guid)
    assert second.guid == first.guid

    await unfulfill(store, first.guid)

    assert await balance(store, wallet) == Decimal("10")


# -- unit of work ---------------------------------------------------------------------------------


async def test_a_callers_unit_of_work_that_raises_discards_the_fulfil(store, provider):
    wallet = await open_wallet(store, ALICE, money(10), source="plan")

    with pytest.raises(RuntimeError):
        async with store.unit_of_work():
            await fulfill(store, "pay", 4, {"user": ALICE}, provider)
            raise RuntimeError("operation failed")

    assert await balance(store, wallet) == Decimal("10")


async def test_a_refused_fulfil_caught_inside_a_callers_unit_of_work_keeps_the_outer_writes(store, provider):
    wallet = await open_wallet(store, ALICE, money(10), source="plan")

    async with store.unit_of_work():
        await fulfill(store, "pay", 4, {"user": ALICE}, provider)
        with pytest.raises(InsufficientBalanceError):
            await fulfill(store, "pay", 100, {"user": ALICE}, provider)

    assert await balance(store, wallet) == Decimal("6")


# -- credit and transfer --------------------------------------------------------------------------


async def test_credit_and_transfer_move_money_and_unfulfill_takes_it_back(store):
    source = await open_wallet(store, ALICE, money(100), source="wallet")
    target = await open_wallet(store, BOB, money(0), source="wallet")

    await credit(store, source.id, 20, actor=ALICE)
    assert await balance(store, source) == Decimal("120")

    result = await transfer(store, source.id, target.id, 30, actor=ALICE)

    assert result.satisfied is True
    assert result.requested == result.granted == Decimal("30")
    assert [(t.kind, t.amount) for t in result.transactions] == [
        (TransactionKind.TRANSFER, Decimal("-30")),
        (TransactionKind.TRANSFER, Decimal("30")),
    ]
    assert await balance(store, source) == Decimal("90")
    assert await balance(store, target) == Decimal("30")

    await unfulfill(store, result.guid)

    assert await balance(store, source) == Decimal("120")
    assert await balance(store, target) == Decimal("0")


async def test_a_transfer_beyond_the_source_balance_is_refused(store):
    source = await open_wallet(store, ALICE, money(10), source="wallet")
    target = await open_wallet(store, BOB, money(0), source="wallet")

    with pytest.raises(InsufficientBalanceError) as raised:
        await transfer(store, source.id, target.id, 11)

    assert raised.value.result.granted == Decimal("10")
    assert await balance(store, source) == Decimal("10")
    assert await balance(store, target) == Decimal("0")


# -- plan switch and archival ---------------------------------------------------------------------


async def test_plan_switch_replaces_plan_wallets_and_leaves_others(store):
    bought = await open_wallet(store, ALICE, money(7), source="purchase")
    [basic] = await plan_switch(store, {"user": ALICE}, [money(10)])

    [pro] = await plan_switch(store, {"user": ALICE}, [money(50)])

    held = {a.wallet.id: a.balance for a in await effective_allowances(store, ALICE, at=utcnow())}
    assert basic.id not in held
    assert held == {bought.id: Decimal("7"), pro.id: Decimal("50")}


async def test_a_scheduled_switch_takes_effect_later_and_can_be_cancelled(store):
    [basic] = await plan_switch(store, {"user": ALICE}, [money(10)])
    switch_at = utcnow() + timedelta(days=30)

    await plan_switch(store, {"user": ALICE}, [money(50)], effective_date=switch_at)

    before = await effective_allowances(store, ALICE)
    after = await effective_allowances(store, ALICE, at=switch_at + timedelta(seconds=1))
    assert [a.balance for a in before] == [Decimal("10")]
    assert [a.balance for a in after] == [Decimal("50")]

    assert await cancel_scheduled_switch(store, {"user": ALICE}) == 1

    after = await effective_allowances(store, ALICE, at=switch_at + timedelta(seconds=1))
    assert [(a.wallet.id, a.balance) for a in after] == [(basic.id, Decimal("10"))]


async def test_archive_expired_wallets_archives_a_closed_wallet(store, provider):
    wallet = await open_wallet(store, ALICE, money(10), source="plan")
    now = utcnow()
    await close_wallet(store, wallet.id, at=now)

    assert await archive_expired_wallets(store, at=now) == 1
    assert await archive_expired_wallets(store, at=now) == 0
    assert await store.get_wallet(wallet.id) is None


# -- allowances.sync ------------------------------------------------------------------------------


def test_sync_re_exports_types_errors_and_definitions():
    from allowances.sync import (  # noqa: F401
        AllowancesError as SyncAllowancesError,
        DictProvider as SyncDictProvider,
        fulfill as sync_fulfill,
        InsufficientBalanceError as SyncInsufficientBalanceError,
        StoreSuspendedError as SyncStoreSuspendedError,
        SubjectRef as SyncSubjectRef,
        WalletSpec as SyncWalletSpec,
    )

    assert SyncSubjectRef is SubjectRef
    assert SyncWalletSpec is WalletSpec
    assert SyncInsufficientBalanceError is InsufficientBalanceError
    assert SyncAllowancesError is AllowancesError
    assert SyncStoreSuspendedError is StoreSuspendedError
    assert SyncDictProvider is DictProvider
    assert not inspect.iscoroutinefunction(sync_fulfill)


def test_sync_functions_keep_the_async_signature_and_docstring():
    for name in ("peek", "fulfill", "unfulfill", "transfer", "plan_switch", "effective_allowances", "add_member"):
        sync_function = getattr(sync, name)
        async_function = getattr(allowances, name)
        assert not inspect.iscoroutinefunction(sync_function), name
        assert inspect.signature(sync_function) == inspect.signature(async_function), name
        assert sync_function.__doc__ == async_function.__doc__, name
        assert sync_function.__name__ == name


def test_sync_right_membership_and_peek(memory_store, provider):
    sync.open_wallet(memory_store, EDITORS, right(), source="grant")

    assert sync.peek(memory_store, "edit_article", 1, {"user": ALICE}, provider).satisfied is False

    sync.add_member(memory_store, ALICE, EDITORS)

    assert sync.groups_of(memory_store, ALICE) == [EDITORS]
    result = sync.fulfill(memory_store, "edit_article", 1, {"user": ALICE}, provider)
    assert result.satisfied is True
    assert [t.actor for t in result.transactions] == [ALICE]

    assert sync.remove_member(memory_store, ALICE, EDITORS) is True
    assert sync.peek(memory_store, "edit_article", 1, {"user": ALICE}, provider).satisfied is False


def test_sync_fulfill_attach_unfulfill(memory_store, provider):
    wallet = sync.open_wallet(memory_store, ALICE, money(10), source="plan")

    result = sync.fulfill(memory_store, "pay", 4, {"user": ALICE}, provider)
    assert sync_balance(memory_store, wallet) == Decimal("6")
    assert [r.guid for r in sync.find_unattached_fulfillment_records(memory_store)] == [result.guid]

    sync.attach(memory_store, result.guid, ObjectRef("shop.order", 1))
    assert sync.find_unattached_fulfillment_records(memory_store) == []

    sync.unfulfill(memory_store, result.guid)
    assert sync_balance(memory_store, wallet) == Decimal("10")


def test_sync_errors_propagate_unchanged(memory_store, provider):
    wallet = sync.open_wallet(memory_store, ALICE, money(3), source="plan")

    with pytest.raises(InsufficientBalanceError) as raised:
        sync.fulfill(memory_store, "pay", 5, {"user": ALICE}, provider)

    assert type(raised.value) is InsufficientBalanceError
    assert raised.value.result.granted == Decimal("3")
    assert sync_balance(memory_store, wallet) == Decimal("3")

    with pytest.raises(MembershipCycleError):
        sync.add_member(memory_store, ALICE, ALICE)


def test_sync_credit_transfer_and_reversal(memory_store):
    source = sync.open_wallet(memory_store, ALICE, money(100), source="wallet")
    target = sync.open_wallet(memory_store, BOB, money(0), source="wallet")

    sync.credit(memory_store, source.id, 20)
    result = sync.transfer(memory_store, source.id, target.id, 30)

    assert sync_balance(memory_store, source) == Decimal("90")
    assert sync_balance(memory_store, target) == Decimal("30")

    sync.unfulfill(memory_store, result.guid)

    assert sync_balance(memory_store, source) == Decimal("120")
    assert sync_balance(memory_store, target) == Decimal("0")


def test_sync_plan_switch_scheduled_and_cancelled(memory_store):
    [basic] = sync.plan_switch(memory_store, {"user": ALICE}, [money(10)])
    switch_at = utcnow() + timedelta(days=30)

    sync.plan_switch(memory_store, {"user": ALICE}, [money(50)], effective_date=switch_at)
    later = switch_at + timedelta(seconds=1)
    assert [a.balance for a in sync.effective_allowances(memory_store, ALICE, at=later)] == [Decimal("50")]

    assert sync.cancel_scheduled_switch(memory_store, {"user": ALICE}) == 1
    assert [a.wallet.id for a in sync.effective_allowances(memory_store, ALICE, at=later)] == [basic.id]


def test_sync_close_and_archive(memory_store):
    wallet = sync.open_wallet(memory_store, ALICE, money(10), source="plan")
    now = utcnow()

    sync.close_wallet(memory_store, wallet.id, at=now)

    assert sync.archive_expired_wallets(memory_store, at=now) == 1
    assert sync.archive_expired_wallets(memory_store, at=now) == 0
    assert asyncio.run(memory_store.get_wallet(wallet.id)) is None


async def test_sync_api_also_works_while_an_event_loop_runs(memory_store, provider):
    wallet = sync.open_wallet(memory_store, ALICE, money(10), source="plan")

    sync.fulfill(memory_store, "pay", 4, {"user": ALICE}, provider)

    assert await balance(memory_store, wallet) == Decimal("6")


def test_async_and_sync_share_the_same_store_state(memory_store, provider):
    wallet = asyncio.run(open_wallet(memory_store, ALICE, money(10), source="plan"))

    sync.fulfill(memory_store, "pay", 4, {"user": ALICE}, provider)

    assert asyncio.run(balance(memory_store, wallet)) == Decimal("6")


# -- a store that suspends ------------------------------------------------------------------------


class _Suspend:
    """An awaitable that yields once to whatever drives the coroutine."""

    def __await__(self):
        yield


class SuspendingStore(MemoryStore):
    """A MemoryStore whose wallet lookup suspends, as a store doing real asynchronous I/O does."""

    def __init__(self):
        super().__init__()
        self.closed_while_suspended = 0

    async def find_wallets(self, **kwargs):
        try:
            await _Suspend()
        except GeneratorExit:
            self.closed_while_suspended += 1
            raise
        return await super().find_wallets(**kwargs)


def test_sync_refuses_a_store_that_suspends(provider):
    store = SuspendingStore()

    with pytest.raises(StoreSuspendedError) as raised:
        sync.peek(store, "pay", 1, {"user": ALICE}, provider)

    assert isinstance(raised.value, AllowancesError)
    assert str(raised.value)
    assert store.closed_while_suspended == 1


def test_sync_refuses_a_suspending_store_in_a_write_and_writes_nothing(provider):
    store = SuspendingStore()
    wallet = asyncio.run(open_wallet(store, ALICE, money(10), source="plan"))

    with pytest.raises(StoreSuspendedError):
        sync.fulfill(store, "pay", 4, {"user": ALICE}, provider)

    assert asyncio.run(store.get_wallet(wallet.id)).balance == Decimal("10")
    assert asyncio.run(find_unattached_fulfillment_records(store)) == []


async def test_the_async_api_runs_on_a_store_that_suspends(provider):
    store = SuspendingStore()
    wallet = await open_wallet(store, ALICE, money(10), source="plan")

    result = await fulfill(store, "pay", 4, {"user": ALICE}, provider)

    assert result.satisfied is True
    assert (await store.get_wallet(wallet.id)).balance == Decimal("6")
    assert store.closed_while_suspended == 0
