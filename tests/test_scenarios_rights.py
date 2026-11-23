"""
Scenarios for rights, groups, the ledger actor and transfers, through the public core API.

A right is a ``GRANT`` wallet accepted by a feature at rate 0: holding a positive balance means
holding the right, and using it costs nothing.
"""

import uuid

from datetime import timedelta
from decimal import Decimal

import pytest

from allowances.backends import MemoryStore
from allowances.definitions import DictProvider
from allowances.errors import (
    AllowancesError,
    InsufficientBalanceError,
    MembershipCycleError,
    UnsuitableWalletError,
    WalletNotFoundError,
)
from allowances.types import ObjectRef, SubjectRef, TransactionKind, WalletSpec, WalletType
from allowances.windows import utcnow
from tests.api import (
    add_member,
    attach,
    close_wallet,
    consumed,
    find_unattached_fulfillment_records,
    fulfill,
    groups_of,
    open_wallet,
    peek,
    remove_member,
    transfer,
    unfulfill,
)
from tests.helpers import transactions_for_wallet

ALICE = SubjectRef("auth.user", 1)
BOB = SubjectRef("auth.user", 2)
EDITORS = SubjectRef("auth.group", 10)
STAFF = SubjectRef("auth.group", 20)
EVERYONE = SubjectRef("auth.group", 30)
GUIDE = SubjectRef("tour.guide", 1)
TOURIST = SubjectRef("tour.tourist", 1)


@pytest.fixture
def store():
    return MemoryStore()


@pytest.fixture
def provider():
    return DictProvider(
        {
            "currencies": {
                "edit_right": {},
                "eur": {},
                "usd": {},
            },
            "features": {
                "edit_article": {
                    "grant": {"currencies": [("edit_right", 0)]},
                    "subjects": {"user"},
                    "fulfillment_order": [("grant", "user")],
                },
                "edit_article_on_credit": {
                    "grant": {"currencies": [("edit_right", 0)]},
                    "subjects": {"user"},
                    "fulfillment_order": [("grant", "user")],
                    "overdraft": True,
                },
                "pay": {
                    "consumable": {"currencies": ["eur"]},
                    "subjects": {"user"},
                    "fulfillment_order": [("consumable", "user")],
                },
            },
        }
    )


async def grant_right(store, subject, *, balance="1", **kwargs):
    return await store.create_wallet(
        subject=subject,
        wallet_type=WalletType.GRANT,
        currency="edit_right",
        balance=Decimal(balance),
        **kwargs,
    )


async def money(store, subject, amount, *, currency="eur"):
    return await store.create_wallet(
        subject=subject,
        wallet_type=WalletType.CONSUMABLE,
        currency=currency,
        balance=Decimal(str(amount)),
    )


async def balance(store, wallet):
    return (await store.get_wallet(wallet.id)).balance


async def entries(store, wallet):
    return await transactions_for_wallet(store, wallet.id)


# -- permanent rights -----------------------------------------------------------------------------


async def test_a_held_right_satisfies_peek_for_any_quantity(store, provider):
    await grant_right(store, ALICE)

    result = await peek(store, "edit_article", 5, {"user": ALICE}, provider)

    assert result.satisfied is True
    assert result.granted == Decimal("5")


async def test_peek_of_a_right_writes_nothing(store, provider):
    wallet = await grant_right(store, ALICE)

    await peek(store, "edit_article", 1, {"user": ALICE}, provider)

    assert await entries(store, wallet) == []
    assert await balance(store, wallet) == Decimal("1")


async def test_fulfilling_a_right_records_a_zero_amount_use_with_the_actor(store, provider):
    wallet = await grant_right(store, ALICE)

    result = await fulfill(store, "edit_article", 1, {"user": ALICE}, provider)

    assert result.satisfied is True
    assert result.guid is not None
    assert [(t.kind, t.amount, t.actor) for t in result.transactions] == [
        (TransactionKind.CONSUMPTION, Decimal("0"), ALICE)
    ]
    assert await balance(store, wallet) == Decimal("1")


async def test_a_right_can_be_used_again_and_again(store, provider):
    wallet = await grant_right(store, ALICE)

    for _ in range(3):
        assert (await fulfill(store, "edit_article", 1, {"user": ALICE}, provider)).satisfied is True

    assert len(await entries(store, wallet)) == 3
    assert await balance(store, wallet) == Decimal("1")


async def test_a_use_of_a_right_attaches_to_the_object_it_was_used_on(store, provider):
    await grant_right(store, ALICE)
    article = ObjectRef("news.article", 7)

    result = await fulfill(store, "edit_article", 1, {"user": ALICE}, provider)
    await attach(store, result.guid, article)

    assert [t.obj for t in await store.transactions_for_record(record_guid=result.guid)] == [article]
    assert await find_unattached_fulfillment_records(store) == []


async def test_an_unattached_use_of_a_right_is_reported_and_can_be_unfulfilled(store, provider):
    wallet = await grant_right(store, ALICE)

    result = await fulfill(store, "edit_article", 1, {"user": ALICE}, provider)

    assert [r.guid for r in await find_unattached_fulfillment_records(store)] == [result.guid]

    await unfulfill(store, result.guid)

    stornos = [t for t in await entries(store, wallet) if t.kind == TransactionKind.STORNO]
    assert [t.amount for t in stornos] == [Decimal("0")]
    assert await balance(store, wallet) == Decimal("1")


async def test_using_a_right_consumes_nothing(store, provider):
    wallet = await grant_right(store, ALICE)
    start = utcnow() - timedelta(minutes=1)

    await fulfill(store, "edit_article", 1, {"user": ALICE}, provider)

    assert await consumed(store, wallet.id, start, utcnow() + timedelta(minutes=1)) == Decimal("0")


async def test_a_zero_balance_means_the_right_is_not_held(store, provider):
    await grant_right(store, ALICE, balance="0")

    assert (await peek(store, "edit_article", 1, {"user": ALICE}, provider)).satisfied is False
    with pytest.raises(InsufficientBalanceError):
        await fulfill(store, "edit_article", 1, {"user": ALICE}, provider)


async def test_overdraft_does_not_confer_a_right(store, provider):
    await grant_right(store, ALICE, balance="0", overdraft_limit=Decimal("100"))

    assert (await peek(store, "edit_article_on_credit", 1, {"user": ALICE}, provider)).satisfied is False
    with pytest.raises(InsufficientBalanceError):
        await fulfill(store, "edit_article_on_credit", 1, {"user": ALICE}, provider)


async def test_without_any_wallet_the_right_is_not_held(store, provider):
    assert (await peek(store, "edit_article", 1, {"user": ALICE}, provider)).satisfied is False


async def test_a_temporary_right_lapses_at_its_valid_until(store, provider):
    now = utcnow()
    await grant_right(store, ALICE, valid_until=now + timedelta(days=1))

    assert (
        await peek(store, "edit_article", 1, {"user": ALICE}, provider, at=now + timedelta(hours=1))
    ).satisfied is True
    assert (
        await peek(store, "edit_article", 1, {"user": ALICE}, provider, at=now + timedelta(days=2))
    ).satisfied is False


# -- revoking with close_wallet -------------------------------------------------------------------


async def test_close_wallet_revokes_a_right(store, provider):
    wallet = await grant_right(store, ALICE)
    now = utcnow()

    closed = await close_wallet(store, wallet.id, at=now)

    assert closed.valid_until == now
    assert (
        await peek(store, "edit_article", 1, {"user": ALICE}, provider, at=now + timedelta(seconds=1))
    ).satisfied is False


async def test_close_wallet_writes_no_ledger_entry_and_keeps_the_balance(store):
    wallet = await grant_right(store, ALICE)

    await close_wallet(store, wallet.id)

    assert await entries(store, wallet) == []
    assert (await store.get_wallet(wallet.id)).balance == Decimal("1")


async def test_close_wallet_does_not_extend_a_wallet_that_ends_earlier(store):
    now = utcnow()
    ends = now + timedelta(days=1)
    wallet = await grant_right(store, ALICE, valid_until=ends)

    closed = await close_wallet(store, wallet.id, at=now + timedelta(days=5))

    assert closed.valid_until == ends


async def test_close_wallet_of_an_unknown_wallet_raises(store):
    with pytest.raises(WalletNotFoundError):
        await close_wallet(store, 999_999)


# -- groups ---------------------------------------------------------------------------------------


async def test_a_member_inherits_a_right_from_its_group(store, provider):
    await grant_right(store, EDITORS)

    assert (await peek(store, "edit_article", 1, {"user": ALICE}, provider)).satisfied is False

    await add_member(store, ALICE, EDITORS)

    assert (await peek(store, "edit_article", 1, {"user": ALICE}, provider)).satisfied is True
    assert (await fulfill(store, "edit_article", 1, {"user": ALICE}, provider)).satisfied is True


async def test_a_member_inherits_a_right_from_its_groups_group(store, provider):
    await grant_right(store, EVERYONE)
    await add_member(store, ALICE, EDITORS)
    await add_member(store, EDITORS, STAFF)
    await add_member(store, STAFF, EVERYONE)

    assert (await peek(store, "edit_article", 1, {"user": ALICE}, provider)).satisfied is True


async def test_a_group_right_is_not_inherited_by_a_non_member(store, provider):
    await grant_right(store, EDITORS)
    await add_member(store, ALICE, EDITORS)

    assert (await peek(store, "edit_article", 1, {"user": BOB}, provider)).satisfied is False


async def test_removing_a_member_takes_the_inherited_right_away(store, provider):
    await grant_right(store, EDITORS)
    await add_member(store, ALICE, EDITORS)

    assert await remove_member(store, ALICE, EDITORS) is True
    assert await remove_member(store, ALICE, EDITORS) is False
    assert (await peek(store, "edit_article", 1, {"user": ALICE}, provider)).satisfied is False


async def test_adding_an_existing_membership_again_changes_nothing(store):
    await add_member(store, ALICE, EDITORS)
    await add_member(store, ALICE, EDITORS)

    assert await groups_of(store, ALICE) == [EDITORS]


async def test_groups_of_lists_direct_groups_before_indirect_ones(store):
    await add_member(store, STAFF, EVERYONE)
    await add_member(store, EDITORS, STAFF)
    await add_member(store, ALICE, EDITORS)

    assert await groups_of(store, ALICE) == [EDITORS, STAFF, EVERYONE]


async def test_a_group_reached_twice_is_listed_once(store):
    await add_member(store, ALICE, EDITORS)
    await add_member(store, ALICE, STAFF)
    await add_member(store, EDITORS, EVERYONE)
    await add_member(store, STAFF, EVERYONE)

    groups = await groups_of(store, ALICE)

    assert sorted(groups[:2], key=str) == sorted([EDITORS, STAFF], key=str)
    assert groups[2:] == [EVERYONE]


async def test_a_subject_cannot_be_its_own_member(store):
    with pytest.raises(MembershipCycleError):
        await add_member(store, EDITORS, EDITORS)


async def test_a_membership_that_would_close_a_cycle_is_rejected(store):
    await add_member(store, EDITORS, STAFF)
    await add_member(store, STAFF, EVERYONE)

    with pytest.raises(MembershipCycleError):
        await add_member(store, EVERYONE, EDITORS)

    assert await groups_of(store, EVERYONE) == []


def test_a_cycle_error_is_an_allowances_error():
    assert issubclass(MembershipCycleError, AllowancesError)


async def test_the_member_is_the_actor_when_the_groups_wallet_pays(store, provider):
    group_wallet = await grant_right(store, EDITORS)
    await add_member(store, ALICE, EDITORS)

    result = await fulfill(store, "edit_article", 1, {"user": ALICE}, provider)

    [entry] = result.transactions
    assert entry.wallet_id == group_wallet.id
    assert entry.actor == ALICE


async def test_the_members_own_wallet_is_drawn_before_the_groups(store, provider):
    own = await money(store, ALICE, 5)
    shared = await money(store, EDITORS, 100)
    await add_member(store, ALICE, EDITORS)

    result = await fulfill(store, "pay", 8, {"user": ALICE}, provider)

    assert [(t.wallet_id, t.amount) for t in result.transactions] == [
        (own.id, Decimal("-5")),
        (shared.id, Decimal("-3")),
    ]
    assert all(t.actor == ALICE for t in result.transactions)


async def test_an_explicit_actor_is_recorded(store, provider):
    await money(store, ALICE, 10)

    result = await fulfill(store, "pay", 1, {"user": ALICE}, provider, actor=BOB)

    assert [t.actor for t in result.transactions] == [BOB]


async def test_the_storno_carries_whoever_reversed_the_use(store, provider):
    wallet = await money(store, ALICE, 10)
    result = await fulfill(store, "pay", 4, {"user": ALICE}, provider)

    await unfulfill(store, result.guid, actor=BOB)

    storno = [t for t in await entries(store, wallet) if t.kind == TransactionKind.STORNO]
    assert [t.actor for t in storno] == [BOB]
    assert await balance(store, wallet) == Decimal("10")


# -- transfers ------------------------------------------------------------------------------------


async def test_transfer_moves_money_between_wallets(store):
    source = await money(store, ALICE, 100)
    target = await money(store, BOB, 0)

    result = await transfer(store, source.id, target.id, 30, actor=ALICE)

    assert await balance(store, source) == Decimal("70")
    assert await balance(store, target) == Decimal("30")
    assert result.requested == result.granted == Decimal("30")
    assert result.satisfied is True
    assert result.guid is not None
    assert [(t.wallet_id, t.kind, t.amount, t.actor) for t in result.transactions] == [
        (source.id, TransactionKind.TRANSFER, Decimal("-30"), ALICE),
        (target.id, TransactionKind.TRANSFER, Decimal("30"), ALICE),
    ]


async def test_transfer_between_different_wallet_types_of_one_currency(store):
    source = await money(store, ALICE, 10)
    target = await store.create_wallet(subject=BOB, wallet_type=WalletType.GRANT, currency="eur", balance=Decimal("0"))

    await transfer(store, source.id, target.id, 4)

    assert await balance(store, target) == Decimal("4")


async def test_transfer_attaches_both_entries_to_the_object(store):
    source = await money(store, ALICE, 10)
    target = await money(store, BOB, 0)
    invoice = ObjectRef("shop.invoice", 3)

    result = await transfer(store, source.id, target.id, 5, obj=invoice)

    assert [t.obj for t in await store.transactions_for_record(record_guid=result.guid)] == [invoice, invoice]


async def test_a_transfer_is_never_reported_as_unattached(store):
    source = await money(store, ALICE, 10)
    target = await money(store, BOB, 0)

    await transfer(store, source.id, target.id, 5)

    assert await find_unattached_fulfillment_records(store) == []


async def test_transfer_between_currencies_is_refused(store):
    source = await money(store, ALICE, 10)
    target = await money(store, BOB, 0, currency="usd")

    with pytest.raises(UnsuitableWalletError):
        await transfer(store, source.id, target.id, 5)

    assert await balance(store, source) == Decimal("10")
    assert await entries(store, source) == []


async def test_transfer_to_the_same_wallet_is_refused(store):
    wallet = await money(store, ALICE, 10)

    with pytest.raises(UnsuitableWalletError):
        await transfer(store, wallet.id, wallet.id, 5)


@pytest.mark.parametrize("amount", ["0", "-5"])
async def test_transfer_of_a_non_positive_amount_is_refused(store, amount):
    source = await money(store, ALICE, 10)
    target = await money(store, BOB, 0)

    with pytest.raises(ValueError):
        await transfer(store, source.id, target.id, amount)


async def test_transfer_beyond_the_sources_balance_is_refused_and_writes_nothing(store):
    source = await money(store, ALICE, 10)
    target = await money(store, BOB, 0)

    with pytest.raises(InsufficientBalanceError):
        await transfer(store, source.id, target.id, 11)

    assert await balance(store, source) == Decimal("10")
    assert await balance(store, target) == Decimal("0")
    assert await entries(store, source) == await entries(store, target) == []


async def test_transfer_never_uses_the_sources_overdraft(store):
    source = await store.create_wallet(
        subject=ALICE,
        wallet_type=WalletType.CONSUMABLE,
        currency="eur",
        balance=Decimal("10"),
        overdraft_limit=Decimal("100"),
    )
    target = await money(store, BOB, 0)

    with pytest.raises(InsufficientBalanceError):
        await transfer(store, source.id, target.id, 20)


async def test_transfer_from_an_unknown_wallet_raises(store):
    target = await money(store, BOB, 0)

    with pytest.raises(WalletNotFoundError):
        await transfer(store, 999_999, target.id, 1)


async def test_unfulfill_reverses_a_transfer(store):
    source = await money(store, ALICE, 100)
    target = await money(store, BOB, 0)
    result = await transfer(store, source.id, target.id, 30)

    await unfulfill(store, result.guid)

    assert await balance(store, source) == Decimal("100")
    assert await balance(store, target) == Decimal("0")
    assert [t.kind for t in await entries(store, source)] == [TransactionKind.TRANSFER, TransactionKind.STORNO]
    assert [t.kind for t in await entries(store, target)] == [TransactionKind.TRANSFER, TransactionKind.STORNO]


async def test_a_transfer_cannot_be_reversed_once_the_target_spent_it(store, provider):
    source = await money(store, ALICE, 100)
    target = await money(store, BOB, 0)
    moved = await transfer(store, source.id, target.id, 30)
    await fulfill(store, "pay", 20, {"user": BOB}, provider)

    with pytest.raises(InsufficientBalanceError):
        await unfulfill(store, moved.guid)

    assert await balance(store, source) == Decimal("70")
    assert await balance(store, target) == Decimal("10")
    assert TransactionKind.STORNO not in [t.kind for t in await entries(store, source) + await entries(store, target)]


async def test_a_transfer_under_a_given_guid_uses_it(store):
    source = await money(store, ALICE, 10)
    target = await money(store, BOB, 0)
    guid = uuid.uuid4()

    result = await transfer(store, source.id, target.id, 2, guid=guid)

    assert result.guid == guid


# -- the tourist guide ----------------------------------------------------------------------------


async def give_tourist_pocket_money(store, guide_wallet, amount):
    async with store.unit_of_work():
        pocket = await open_wallet(store, TOURIST, WalletSpec(WalletType.CONSUMABLE, "eur"), source="tour")
        await transfer(store, guide_wallet.id, pocket.id, amount, actor=GUIDE)
    return pocket


async def tourist_wallets(store):
    return await store.find_wallets(
        subjects=[TOURIST], wallet_types=[WalletType.CONSUMABLE, WalletType.GRANT], currencies=None, at=utcnow()
    )


async def test_a_guide_funds_a_tourist_who_spends_and_returns_the_rest(store, provider):
    budget = await money(store, GUIDE, 100)

    pocket = await give_tourist_pocket_money(store, budget, 10)

    assert await balance(store, budget) == Decimal("90")
    assert await balance(store, pocket) == Decimal("10")

    await fulfill(store, "pay", 7, {"user": TOURIST}, provider)
    assert await balance(store, pocket) == Decimal("3")

    await transfer(store, pocket.id, budget.id, await balance(store, pocket), actor=GUIDE)

    assert await balance(store, pocket) == Decimal("0")
    assert await balance(store, budget) == Decimal("93")


async def test_a_tourist_cannot_spend_more_than_the_guide_gave(store, provider):
    budget = await money(store, GUIDE, 100)
    await give_tourist_pocket_money(store, budget, 10)

    with pytest.raises(InsufficientBalanceError):
        await fulfill(store, "pay", 11, {"user": TOURIST}, provider)

    assert await balance(store, budget) == Decimal("90")


async def test_no_wallet_is_opened_when_the_guides_budget_cannot_fund_it(store):
    budget = await money(store, GUIDE, 5)

    with pytest.raises(InsufficientBalanceError):
        await give_tourist_pocket_money(store, budget, 10)

    assert await balance(store, budget) == Decimal("5")
    assert await tourist_wallets(store) == []
