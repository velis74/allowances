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
from allowances.engine import (
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
from allowances.errors import (
    AllowancesError,
    InsufficientBalanceError,
    MembershipCycleError,
    UnsuitableWalletError,
    WalletNotFoundError,
)
from allowances.types import ObjectRef, SubjectRef, TransactionKind, WalletSpec, WalletType
from allowances.windows import utcnow
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


def grant_right(store, subject, *, balance="1", **kwargs):
    return store.create_wallet(
        subject=subject,
        wallet_type=WalletType.GRANT,
        currency="edit_right",
        balance=Decimal(balance),
        **kwargs,
    )


def money(store, subject, amount, *, currency="eur"):
    return store.create_wallet(
        subject=subject,
        wallet_type=WalletType.CONSUMABLE,
        currency=currency,
        balance=Decimal(str(amount)),
    )


def balance(store, wallet):
    return store.get_wallet(wallet.id).balance


def entries(store, wallet):
    return transactions_for_wallet(store, wallet.id)


# -- permanent rights -----------------------------------------------------------------------------


def test_a_held_right_satisfies_peek_for_any_quantity(store, provider):
    grant_right(store, ALICE)

    result = peek(store, "edit_article", 5, {"user": ALICE}, provider)

    assert result.satisfied is True
    assert result.granted == Decimal("5")


def test_peek_of_a_right_writes_nothing(store, provider):
    wallet = grant_right(store, ALICE)

    peek(store, "edit_article", 1, {"user": ALICE}, provider)

    assert entries(store, wallet) == []
    assert balance(store, wallet) == Decimal("1")


def test_fulfilling_a_right_records_a_zero_amount_use_with_the_actor(store, provider):
    wallet = grant_right(store, ALICE)

    result = fulfill(store, "edit_article", 1, {"user": ALICE}, provider)

    assert result.satisfied is True
    assert result.guid is not None
    assert [(t.kind, t.amount, t.actor) for t in result.transactions] == [
        (TransactionKind.CONSUMPTION, Decimal("0"), ALICE)
    ]
    assert balance(store, wallet) == Decimal("1")


def test_a_right_can_be_used_again_and_again(store, provider):
    wallet = grant_right(store, ALICE)

    for _ in range(3):
        assert fulfill(store, "edit_article", 1, {"user": ALICE}, provider).satisfied is True

    assert len(entries(store, wallet)) == 3
    assert balance(store, wallet) == Decimal("1")


def test_a_use_of_a_right_attaches_to_the_object_it_was_used_on(store, provider):
    grant_right(store, ALICE)
    article = ObjectRef("news.article", 7)

    result = fulfill(store, "edit_article", 1, {"user": ALICE}, provider)
    attach(store, result.guid, article)

    assert [t.obj for t in store.transactions_for_record(record_guid=result.guid)] == [article]
    assert find_unattached_fulfillment_records(store) == []


def test_an_unattached_use_of_a_right_is_reported_and_can_be_unfulfilled(store, provider):
    wallet = grant_right(store, ALICE)

    result = fulfill(store, "edit_article", 1, {"user": ALICE}, provider)

    assert [r.guid for r in find_unattached_fulfillment_records(store)] == [result.guid]

    unfulfill(store, result.guid)

    stornos = [t for t in entries(store, wallet) if t.kind == TransactionKind.STORNO]
    assert [t.amount for t in stornos] == [Decimal("0")]
    assert balance(store, wallet) == Decimal("1")


def test_using_a_right_consumes_nothing(store, provider):
    wallet = grant_right(store, ALICE)
    start = utcnow() - timedelta(minutes=1)

    fulfill(store, "edit_article", 1, {"user": ALICE}, provider)

    assert consumed(store, wallet.id, start, utcnow() + timedelta(minutes=1)) == Decimal("0")


def test_a_zero_balance_means_the_right_is_not_held(store, provider):
    grant_right(store, ALICE, balance="0")

    assert peek(store, "edit_article", 1, {"user": ALICE}, provider).satisfied is False
    with pytest.raises(InsufficientBalanceError):
        fulfill(store, "edit_article", 1, {"user": ALICE}, provider)


def test_overdraft_does_not_confer_a_right(store, provider):
    grant_right(store, ALICE, balance="0", overdraft_limit=Decimal("100"))

    assert peek(store, "edit_article_on_credit", 1, {"user": ALICE}, provider).satisfied is False
    with pytest.raises(InsufficientBalanceError):
        fulfill(store, "edit_article_on_credit", 1, {"user": ALICE}, provider)


def test_without_any_wallet_the_right_is_not_held(store, provider):
    assert peek(store, "edit_article", 1, {"user": ALICE}, provider).satisfied is False


def test_a_temporary_right_lapses_at_its_valid_until(store, provider):
    now = utcnow()
    grant_right(store, ALICE, valid_until=now + timedelta(days=1))

    assert peek(store, "edit_article", 1, {"user": ALICE}, provider, at=now + timedelta(hours=1)).satisfied is True
    assert peek(store, "edit_article", 1, {"user": ALICE}, provider, at=now + timedelta(days=2)).satisfied is False


# -- revoking with close_wallet -------------------------------------------------------------------


def test_close_wallet_revokes_a_right(store, provider):
    wallet = grant_right(store, ALICE)
    now = utcnow()

    closed = close_wallet(store, wallet.id, at=now)

    assert closed.valid_until == now
    assert peek(store, "edit_article", 1, {"user": ALICE}, provider, at=now + timedelta(seconds=1)).satisfied is False


def test_close_wallet_writes_no_ledger_entry_and_keeps_the_balance(store):
    wallet = grant_right(store, ALICE)

    close_wallet(store, wallet.id)

    assert entries(store, wallet) == []
    assert store.get_wallet(wallet.id).balance == Decimal("1")


def test_close_wallet_does_not_extend_a_wallet_that_ends_earlier(store):
    now = utcnow()
    ends = now + timedelta(days=1)
    wallet = grant_right(store, ALICE, valid_until=ends)

    closed = close_wallet(store, wallet.id, at=now + timedelta(days=5))

    assert closed.valid_until == ends


def test_close_wallet_of_an_unknown_wallet_raises(store):
    with pytest.raises(WalletNotFoundError):
        close_wallet(store, 999_999)


# -- groups ---------------------------------------------------------------------------------------


def test_a_member_inherits_a_right_from_its_group(store, provider):
    grant_right(store, EDITORS)

    assert peek(store, "edit_article", 1, {"user": ALICE}, provider).satisfied is False

    add_member(store, ALICE, EDITORS)

    assert peek(store, "edit_article", 1, {"user": ALICE}, provider).satisfied is True
    assert fulfill(store, "edit_article", 1, {"user": ALICE}, provider).satisfied is True


def test_a_member_inherits_a_right_from_its_groups_group(store, provider):
    grant_right(store, EVERYONE)
    add_member(store, ALICE, EDITORS)
    add_member(store, EDITORS, STAFF)
    add_member(store, STAFF, EVERYONE)

    assert peek(store, "edit_article", 1, {"user": ALICE}, provider).satisfied is True


def test_a_group_right_is_not_inherited_by_a_non_member(store, provider):
    grant_right(store, EDITORS)
    add_member(store, ALICE, EDITORS)

    assert peek(store, "edit_article", 1, {"user": BOB}, provider).satisfied is False


def test_removing_a_member_takes_the_inherited_right_away(store, provider):
    grant_right(store, EDITORS)
    add_member(store, ALICE, EDITORS)

    assert remove_member(store, ALICE, EDITORS) is True
    assert remove_member(store, ALICE, EDITORS) is False
    assert peek(store, "edit_article", 1, {"user": ALICE}, provider).satisfied is False


def test_adding_an_existing_membership_again_changes_nothing(store):
    add_member(store, ALICE, EDITORS)
    add_member(store, ALICE, EDITORS)

    assert groups_of(store, ALICE) == [EDITORS]


def test_groups_of_lists_direct_groups_before_indirect_ones(store):
    add_member(store, STAFF, EVERYONE)
    add_member(store, EDITORS, STAFF)
    add_member(store, ALICE, EDITORS)

    assert groups_of(store, ALICE) == [EDITORS, STAFF, EVERYONE]


def test_a_group_reached_twice_is_listed_once(store):
    add_member(store, ALICE, EDITORS)
    add_member(store, ALICE, STAFF)
    add_member(store, EDITORS, EVERYONE)
    add_member(store, STAFF, EVERYONE)

    groups = groups_of(store, ALICE)

    assert sorted(groups[:2], key=str) == sorted([EDITORS, STAFF], key=str)
    assert groups[2:] == [EVERYONE]


def test_a_subject_cannot_be_its_own_member(store):
    with pytest.raises(MembershipCycleError):
        add_member(store, EDITORS, EDITORS)


def test_a_membership_that_would_close_a_cycle_is_rejected(store):
    add_member(store, EDITORS, STAFF)
    add_member(store, STAFF, EVERYONE)

    with pytest.raises(MembershipCycleError):
        add_member(store, EVERYONE, EDITORS)

    assert groups_of(store, EVERYONE) == []


def test_a_cycle_error_is_an_allowances_error():
    assert issubclass(MembershipCycleError, AllowancesError)


def test_the_member_is_the_actor_when_the_groups_wallet_pays(store, provider):
    group_wallet = grant_right(store, EDITORS)
    add_member(store, ALICE, EDITORS)

    result = fulfill(store, "edit_article", 1, {"user": ALICE}, provider)

    [entry] = result.transactions
    assert entry.wallet_id == group_wallet.id
    assert entry.actor == ALICE


def test_the_members_own_wallet_is_drawn_before_the_groups(store, provider):
    own = money(store, ALICE, 5)
    shared = money(store, EDITORS, 100)
    add_member(store, ALICE, EDITORS)

    result = fulfill(store, "pay", 8, {"user": ALICE}, provider)

    assert [(t.wallet_id, t.amount) for t in result.transactions] == [
        (own.id, Decimal("-5")),
        (shared.id, Decimal("-3")),
    ]
    assert all(t.actor == ALICE for t in result.transactions)


def test_an_explicit_actor_is_recorded(store, provider):
    money(store, ALICE, 10)

    result = fulfill(store, "pay", 1, {"user": ALICE}, provider, actor=BOB)

    assert [t.actor for t in result.transactions] == [BOB]


def test_the_storno_carries_whoever_reversed_the_use(store, provider):
    wallet = money(store, ALICE, 10)
    result = fulfill(store, "pay", 4, {"user": ALICE}, provider)

    unfulfill(store, result.guid, actor=BOB)

    storno = [t for t in entries(store, wallet) if t.kind == TransactionKind.STORNO]
    assert [t.actor for t in storno] == [BOB]
    assert balance(store, wallet) == Decimal("10")


# -- transfers ------------------------------------------------------------------------------------


def test_transfer_moves_money_between_wallets(store):
    source = money(store, ALICE, 100)
    target = money(store, BOB, 0)

    result = transfer(store, source.id, target.id, 30, actor=ALICE)

    assert balance(store, source) == Decimal("70")
    assert balance(store, target) == Decimal("30")
    assert result.requested == result.granted == Decimal("30")
    assert result.satisfied is True
    assert result.guid is not None
    assert [(t.wallet_id, t.kind, t.amount, t.actor) for t in result.transactions] == [
        (source.id, TransactionKind.TRANSFER, Decimal("-30"), ALICE),
        (target.id, TransactionKind.TRANSFER, Decimal("30"), ALICE),
    ]


def test_transfer_between_different_wallet_types_of_one_currency(store):
    source = money(store, ALICE, 10)
    target = store.create_wallet(subject=BOB, wallet_type=WalletType.GRANT, currency="eur", balance=Decimal("0"))

    transfer(store, source.id, target.id, 4)

    assert balance(store, target) == Decimal("4")


def test_transfer_attaches_both_entries_to_the_object(store):
    source = money(store, ALICE, 10)
    target = money(store, BOB, 0)
    invoice = ObjectRef("shop.invoice", 3)

    result = transfer(store, source.id, target.id, 5, obj=invoice)

    assert [t.obj for t in store.transactions_for_record(record_guid=result.guid)] == [invoice, invoice]


def test_a_transfer_is_never_reported_as_unattached(store):
    source = money(store, ALICE, 10)
    target = money(store, BOB, 0)

    transfer(store, source.id, target.id, 5)

    assert find_unattached_fulfillment_records(store) == []


def test_transfer_between_currencies_is_refused(store):
    source = money(store, ALICE, 10)
    target = money(store, BOB, 0, currency="usd")

    with pytest.raises(UnsuitableWalletError):
        transfer(store, source.id, target.id, 5)

    assert balance(store, source) == Decimal("10")
    assert entries(store, source) == []


def test_transfer_to_the_same_wallet_is_refused(store):
    wallet = money(store, ALICE, 10)

    with pytest.raises(UnsuitableWalletError):
        transfer(store, wallet.id, wallet.id, 5)


@pytest.mark.parametrize("amount", ["0", "-5"])
def test_transfer_of_a_non_positive_amount_is_refused(store, amount):
    source = money(store, ALICE, 10)
    target = money(store, BOB, 0)

    with pytest.raises(ValueError):
        transfer(store, source.id, target.id, amount)


def test_transfer_beyond_the_sources_balance_is_refused_and_writes_nothing(store):
    source = money(store, ALICE, 10)
    target = money(store, BOB, 0)

    with pytest.raises(InsufficientBalanceError):
        transfer(store, source.id, target.id, 11)

    assert balance(store, source) == Decimal("10")
    assert balance(store, target) == Decimal("0")
    assert entries(store, source) == entries(store, target) == []


def test_transfer_never_uses_the_sources_overdraft(store):
    source = store.create_wallet(
        subject=ALICE,
        wallet_type=WalletType.CONSUMABLE,
        currency="eur",
        balance=Decimal("10"),
        overdraft_limit=Decimal("100"),
    )
    target = money(store, BOB, 0)

    with pytest.raises(InsufficientBalanceError):
        transfer(store, source.id, target.id, 20)


def test_transfer_from_an_unknown_wallet_raises(store):
    target = money(store, BOB, 0)

    with pytest.raises(WalletNotFoundError):
        transfer(store, 999_999, target.id, 1)


def test_unfulfill_reverses_a_transfer(store):
    source = money(store, ALICE, 100)
    target = money(store, BOB, 0)
    result = transfer(store, source.id, target.id, 30)

    unfulfill(store, result.guid)

    assert balance(store, source) == Decimal("100")
    assert balance(store, target) == Decimal("0")
    assert [t.kind for t in entries(store, source)] == [TransactionKind.TRANSFER, TransactionKind.STORNO]
    assert [t.kind for t in entries(store, target)] == [TransactionKind.TRANSFER, TransactionKind.STORNO]


def test_a_transfer_cannot_be_reversed_once_the_target_spent_it(store, provider):
    source = money(store, ALICE, 100)
    target = money(store, BOB, 0)
    moved = transfer(store, source.id, target.id, 30)
    fulfill(store, "pay", 20, {"user": BOB}, provider)

    with pytest.raises(InsufficientBalanceError):
        unfulfill(store, moved.guid)

    assert balance(store, source) == Decimal("70")
    assert balance(store, target) == Decimal("10")
    assert TransactionKind.STORNO not in [t.kind for t in entries(store, source) + entries(store, target)]


def test_a_transfer_under_a_given_guid_uses_it(store):
    source = money(store, ALICE, 10)
    target = money(store, BOB, 0)
    guid = uuid.uuid4()

    result = transfer(store, source.id, target.id, 2, guid=guid)

    assert result.guid == guid


# -- the tourist guide ----------------------------------------------------------------------------


def give_tourist_pocket_money(store, guide_wallet, amount):
    with store.unit_of_work():
        pocket = open_wallet(store, TOURIST, WalletSpec(WalletType.CONSUMABLE, "eur"), source="tour")
        transfer(store, guide_wallet.id, pocket.id, amount, actor=GUIDE)
    return pocket


def tourist_wallets(store):
    return store.find_wallets(
        subjects=[TOURIST], wallet_types=[WalletType.CONSUMABLE, WalletType.GRANT], currencies=None, at=utcnow()
    )


def test_a_guide_funds_a_tourist_who_spends_and_returns_the_rest(store, provider):
    budget = money(store, GUIDE, 100)

    pocket = give_tourist_pocket_money(store, budget, 10)

    assert balance(store, budget) == Decimal("90")
    assert balance(store, pocket) == Decimal("10")

    fulfill(store, "pay", 7, {"user": TOURIST}, provider)
    assert balance(store, pocket) == Decimal("3")

    transfer(store, pocket.id, budget.id, balance(store, pocket), actor=GUIDE)

    assert balance(store, pocket) == Decimal("0")
    assert balance(store, budget) == Decimal("93")


def test_a_tourist_cannot_spend_more_than_the_guide_gave(store, provider):
    budget = money(store, GUIDE, 100)
    give_tourist_pocket_money(store, budget, 10)

    with pytest.raises(InsufficientBalanceError):
        fulfill(store, "pay", 11, {"user": TOURIST}, provider)

    assert balance(store, budget) == Decimal("90")


def test_no_wallet_is_opened_when_the_guides_budget_cannot_fund_it(store):
    budget = money(store, GUIDE, 5)

    with pytest.raises(InsufficientBalanceError):
        give_tourist_pocket_money(store, budget, 10)

    assert balance(store, budget) == Decimal("5")
    assert tourist_wallets(store) == []
