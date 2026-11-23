"""``effective_allowances``: every live wallet of a subject and of its groups, with holder, depth and balance."""

from datetime import timedelta
from decimal import Decimal

from allowances.types import Allowance, SubjectRef, SwitchPolicy, TransactionKind, WalletSpec, WalletType
from allowances.windows import utcnow
from tests.api import add_member, close_wallet, effective_allowances, plan_switch
from tests.conftest import open_wallet
from tests.helpers import transaction_count

USER = SubjectRef("auth.user", 1)
OTHER_USER = SubjectRef("auth.user", 2)
TEAM = SubjectRef("team", 1)
CLUB = SubjectRef("club", 1)
DEPARTMENT = SubjectRef("department", 1)
COMPANY = SubjectRef("company", 1)


def _summary(allowances: list[Allowance]) -> list[tuple]:
    return [(a.holder, a.depth, a.wallet.currency, a.balance) for a in allowances]


async def test_lists_the_subjects_own_wallets_of_both_types(store):
    credits = await open_wallet(store, USER, balance="10")
    right = await open_wallet(
        store, USER, currency="perm:news.change_article", balance="1", wallet_type=WalletType.GRANT
    )
    await open_wallet(store, OTHER_USER, balance="99")

    allowances = await effective_allowances(store, USER)

    assert [a.wallet.id for a in allowances] == [credits.id, right.id]
    assert _summary(allowances) == [
        (USER, 0, "credits", Decimal("10")),
        (USER, 0, "perm:news.change_article", Decimal("1")),
    ]


async def test_lists_wallets_of_groups_and_nested_groups_by_depth_then_group_order(store):
    await add_member(store, USER, TEAM)
    await add_member(store, USER, CLUB)
    await add_member(store, TEAM, DEPARTMENT)
    await add_member(store, DEPARTMENT, COMPANY)
    await open_wallet(store, COMPANY, balance="4")
    await open_wallet(store, DEPARTMENT, balance="3")
    await open_wallet(store, CLUB, balance="2")
    await open_wallet(store, TEAM, balance="1")
    await open_wallet(store, USER, balance="0")

    assert _summary(await effective_allowances(store, USER)) == [
        (USER, 0, "credits", Decimal("0")),
        (TEAM, 1, "credits", Decimal("1")),
        (CLUB, 1, "credits", Decimal("2")),
        (DEPARTMENT, 2, "credits", Decimal("3")),
        (COMPANY, 3, "credits", Decimal("4")),
    ]


async def test_a_group_queried_directly_sees_only_itself_and_its_own_groups(store):
    await add_member(store, USER, TEAM)
    await add_member(store, TEAM, DEPARTMENT)
    await open_wallet(store, USER, balance="1")
    await open_wallet(store, TEAM, balance="2")
    await open_wallet(store, DEPARTMENT, balance="3")

    assert _summary(await effective_allowances(store, TEAM)) == [
        (TEAM, 0, "credits", Decimal("2")),
        (DEPARTMENT, 1, "credits", Decimal("3")),
    ]


async def test_filters_by_wallet_type_and_currency(store):
    await add_member(store, USER, TEAM)
    credits = await open_wallet(store, USER, balance="10")
    tokens = await open_wallet(store, TEAM, currency="tokens", balance="5")
    right = await open_wallet(
        store, TEAM, currency="perm:news.change_article", balance="1", wallet_type=WalletType.GRANT
    )

    async def ids(**filters):
        return [a.wallet.id for a in await effective_allowances(store, USER, **filters)]

    assert await ids(wallet_types=[WalletType.GRANT]) == [right.id]
    assert await ids(wallet_types=[WalletType.CONSUMABLE]) == [credits.id, tokens.id]
    assert await ids(currencies=["tokens", "perm:news.change_article"]) == [tokens.id, right.id]
    assert await ids(wallet_types=[WalletType.CONSUMABLE], currencies=["perm:news.change_article"]) == []


async def test_leaves_out_expired_closed_and_not_yet_valid_wallets(store):
    now = utcnow()
    live = await open_wallet(store, USER, balance="1", valid_until=now + timedelta(days=1))
    await open_wallet(store, USER, balance="2", valid_until=now - timedelta(hours=1))
    await open_wallet(store, USER, balance="3", valid_from=now + timedelta(days=1))
    closed = await open_wallet(store, USER, balance="4", valid_from=now - timedelta(days=1))
    await close_wallet(store, closed.id, at=now)

    assert [a.wallet.id for a in await effective_allowances(store, USER, at=now + timedelta(seconds=1))] == [live.id]
    assert [a.wallet.id for a in await effective_allowances(store, USER, at=now - timedelta(seconds=1))] == [
        live.id,
        closed.id,
    ]


async def test_lists_zero_and_negative_balances(store):
    await open_wallet(store, USER, balance="0")
    await open_wallet(store, USER, balance="-5", overdraft_limit=Decimal("10"))

    assert [a.balance for a in await effective_allowances(store, USER)] == [Decimal("0"), Decimal("-5")]


async def test_the_balance_of_a_windowed_wallet_is_its_window_balance(store):
    now = utcnow()
    wallet = await open_wallet(store, USER, balance="100", reset_config={"window_seconds": 86400})
    await store.create_transaction(
        wallet_id=wallet.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-40"), at=now - timedelta(days=2)
    )
    await store.create_transaction(
        wallet_id=wallet.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-10"), at=now - timedelta(hours=1)
    )

    [allowance] = await effective_allowances(store, USER, at=now)

    assert allowance.balance == Decimal("90")
    assert allowance.wallet.balance == Decimal("100")


async def test_a_due_scheduled_adjustment_is_shown_without_being_written(store):
    spec = WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("100"), on_switch=SwitchPolicy.TOP_UP)
    [wallet] = await plan_switch(store, {"user": USER}, [spec])
    when = utcnow() + timedelta(days=7)
    await plan_switch(
        store,
        {"user": USER},
        [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("300"), on_switch=SwitchPolicy.TOP_UP)],
        effective_date=when,
    )
    entries = await transaction_count(store)

    [before] = await effective_allowances(store, USER, at=when - timedelta(seconds=1))
    [after] = await effective_allowances(store, USER, at=when + timedelta(hours=1))

    assert (before.balance, before.wallet.allotment) == (Decimal("100"), Decimal("100"))
    assert (after.balance, after.wallet.allotment, after.wallet.scheduled) == (Decimal("300"), Decimal("300"), None)
    stored = await store.get_wallet(wallet.id)
    assert (stored.balance, stored.scheduled is not None) == (Decimal("100"), True)
    assert await transaction_count(store) == entries


async def test_terminates_on_cyclic_memberships(store):
    async with store.unit_of_work():
        await store.add_membership(member=USER, group=TEAM)
        await store.add_membership(member=TEAM, group=DEPARTMENT)
        await store.add_membership(member=DEPARTMENT, group=USER)
    await open_wallet(store, USER, balance="1")
    await open_wallet(store, TEAM, balance="2")
    await open_wallet(store, DEPARTMENT, balance="3")

    assert [(a.holder, a.depth) for a in await effective_allowances(store, USER)] == [
        (USER, 0),
        (TEAM, 1),
        (DEPARTMENT, 2),
    ]


async def test_at_selects_the_wallets_valid_in_the_past_or_the_future(store):
    now = utcnow()
    ended = await open_wallet(
        store, USER, balance="1", valid_from=now - timedelta(days=10), valid_until=now - timedelta(days=5)
    )
    current = await open_wallet(store, USER, balance="2", valid_from=now - timedelta(days=1))
    upcoming = await open_wallet(store, USER, balance="3", valid_from=now + timedelta(days=5))

    async def ids(at):
        return {a.wallet.id for a in await effective_allowances(store, USER, at=at)}

    assert await ids(now - timedelta(days=7)) == {ended.id}
    assert await ids(now) == {current.id}
    assert await ids(now + timedelta(days=6)) == {current.id, upcoming.id}


async def test_writes_nothing(store):
    await add_member(store, USER, TEAM)
    await open_wallet(store, TEAM, balance="5")
    entries = await transaction_count(store)

    await effective_allowances(store, USER)

    assert await transaction_count(store) == entries
