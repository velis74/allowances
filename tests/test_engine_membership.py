"""Memberships: ``add_member``, ``remove_member``, ``groups_of`` and the expansion of subjects."""

from decimal import Decimal

import pytest

from allowances.errors import MembershipCycleError
from allowances.types import SubjectRef, WalletType
from tests.api import add_member, fulfill, groups_of, peek, remove_member
from tests.conftest import CREDITS, open_wallet, provider_with
from tests.helpers import add_modifier

USER = SubjectRef("auth.user", 1)
OTHER_USER = SubjectRef("auth.user", 2)
TEAM = SubjectRef("team", 1)
DEPARTMENT = SubjectRef("department", 1)
COMPANY = SubjectRef("company", 1)
CLUB = SubjectRef("club", 1)


def _provider(order, subjects=("user", "team")):
    return provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": set(subjects),
                "fulfillment_order": order,
            }
        },
        currencies=CREDITS,
    )


# -- groups_of / add_member / remove_member ----------------------------------------------------------


async def test_groups_of_walks_breadth_first_and_lists_each_group_once(store):
    await add_member(store, USER, TEAM)
    await add_member(store, USER, CLUB)
    await add_member(store, TEAM, DEPARTMENT)
    await add_member(store, CLUB, DEPARTMENT)
    await add_member(store, DEPARTMENT, COMPANY)

    assert await groups_of(store, USER) == [TEAM, CLUB, DEPARTMENT, COMPANY]
    assert await groups_of(store, COMPANY) == []


async def test_groups_of_terminates_on_cyclic_data(store):
    async with store.unit_of_work():
        await store.add_membership(member=USER, group=TEAM)
        await store.add_membership(member=TEAM, group=DEPARTMENT)
        await store.add_membership(member=DEPARTMENT, group=USER)

    assert await groups_of(store, USER) == [TEAM, DEPARTMENT]


@pytest.mark.parametrize(
    "member, group",
    [(TEAM, TEAM), (DEPARTMENT, USER), (COMPANY, TEAM)],
    ids=["itself", "a member of the member", "a transitive member of the member"],
)
async def test_add_member_rejects_a_cycle(store, member, group):
    await add_member(store, USER, TEAM)
    await add_member(store, TEAM, DEPARTMENT)
    await add_member(store, DEPARTMENT, COMPANY)

    with pytest.raises(MembershipCycleError):
        await add_member(store, member, group)


async def test_add_member_twice_keeps_one_membership(store):
    await add_member(store, USER, TEAM)
    await add_member(store, USER, TEAM)

    assert await store.memberships_of(members=[USER]) == [(USER, TEAM)]


async def test_remove_member_reports_whether_the_membership_existed(store):
    await add_member(store, USER, TEAM)

    assert await remove_member(store, USER, TEAM) is True
    assert await remove_member(store, USER, TEAM) is False
    assert await groups_of(store, USER) == []


# -- expansion ---------------------------------------------------------------------------------------


async def test_a_member_draws_from_its_groups_wallet_and_is_the_actor(store):
    await add_member(store, USER, TEAM)
    team_wallet = await open_wallet(store, TEAM, balance="10")

    result = await fulfill(store, "send_email", 3, {"user": USER}, _provider([("consumable", "user")]))

    assert [(t.wallet_id, t.amount, t.actor) for t in result.transactions] == [(team_wallet.id, Decimal("-3"), USER)]


async def test_a_step_walks_the_subject_before_its_groups_by_depth(store):
    await add_member(store, USER, TEAM)
    await add_member(store, TEAM, COMPANY)
    company = await open_wallet(store, COMPANY, balance="10")
    team = await open_wallet(store, TEAM, balance="1", overdraft_limit=Decimal("0"))
    own = await open_wallet(store, USER, balance="1", overdraft_limit=Decimal("0"))

    result = await fulfill(store, "send_email", 3, {"user": USER}, _provider([("consumable", "user")]))

    assert [t.wallet_id for t in result.transactions] == [own.id, team.id, company.id]


async def test_a_step_without_a_subject_walks_every_expanded_subject(store):
    await add_member(store, USER, TEAM)
    team = await open_wallet(store, TEAM, balance="10")

    provider = _provider([("consumable", None)])

    assert (await peek(store, "send_email", 3, {"user": USER}, provider)).satisfied is True
    result = await fulfill(store, "send_email", 3, {"user": USER}, provider)
    assert [t.wallet_id for t in result.transactions] == [team.id]


async def test_a_callable_order_receives_the_groups_wallets(store):
    await add_member(store, USER, TEAM)
    team = await open_wallet(store, TEAM, balance="10")
    provider = _provider("tests.fixtures_ordering.order_wallets")

    result = await fulfill(store, "send_email", 3, {"user": USER}, provider)

    assert [t.wallet_id for t in result.transactions] == [team.id]


async def test_a_groups_wallet_is_drawn_once_when_two_keys_share_the_group(store):
    await add_member(store, USER, TEAM)
    await add_member(store, OTHER_USER, TEAM)
    team = await open_wallet(store, TEAM, balance="2", overdraft_limit=Decimal("0"))

    result = await fulfill(
        store,
        "send_email",
        3,
        {"user": USER, "team": OTHER_USER},
        _provider([("consumable", "user"), ("consumable", "team")]),
        allow_partial=True,
    )

    assert [(t.wallet_id, t.amount) for t in result.transactions] == [(team.id, Decimal("-2"))]
    assert result.transactions[0].actor is None


async def test_a_groups_modifier_applies_to_its_members(store):
    await add_member(store, USER, TEAM)
    await open_wallet(store, USER, balance="10")
    await add_modifier(store, subject=TEAM, feature="send_email", factor="0.5")

    result = await fulfill(store, "send_email", 4, {"user": USER}, _provider([("consumable", "user")]))

    assert result.modifier_applied == Decimal("0.5")
    assert [t.amount for t in result.transactions] == [Decimal("-2")]


async def test_a_groups_right_is_held_by_its_members(store):
    provider = provider_with(
        features={
            "edit": {
                "grant": {"currencies": [("edit_right", 0)]},
                "subjects": {"user"},
                "fulfillment_order": [("grant", None)],
            }
        },
        currencies={"edit_right": {}},
    )
    await add_member(store, USER, TEAM)
    await add_member(store, TEAM, COMPANY)
    await open_wallet(store, COMPANY, currency="edit_right", balance="1", wallet_type=WalletType.GRANT)

    assert (await peek(store, "edit", 1, {"user": USER}, provider)).satisfied is True
    assert (await peek(store, "edit", 1, {"user": OTHER_USER}, provider)).satisfied is False
