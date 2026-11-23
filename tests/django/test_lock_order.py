"""
Operations that lock several wallets take the locks in one global order, so two of them can never
wait for each other in a cycle.
"""

from decimal import Decimal

from allowances.errors import InsufficientBalanceError
from allowances.types import SubjectRef
from tests.api import fulfill
from tests.conftest import CREDITS, provider_with
from tests.django.test_concurrency import run_concurrently, WORKERS


async def test_fulfill_with_opposite_orders_over_the_same_wallets_does_not_deadlock(store_factory):
    """Two features lock the same user and team wallets, each walking them in the opposite order."""
    provider = provider_with(
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
    user, team = SubjectRef("auth.user", 1), SubjectRef("team", 1)
    store = store_factory()
    for subject in (user, team):
        await store.create_wallet(
            subject=subject,
            wallet_type="consumable",
            currency="credits",
            balance=Decimal("1000"),
            overdraft_limit=Decimal("0"),
        )

    stores = [store_factory(index) for index in range(WORKERS)]

    async def draw(index):
        feature = "user_first" if index % 2 else "team_first"
        outcomes = []
        for _ in range(20):
            try:
                outcomes.append(await fulfill(stores[index], feature, 3, {"user": user, "team": team}, provider))
            except InsufficientBalanceError:
                outcomes.append(None)
        return outcomes

    outcomes = await run_concurrently(draw)

    errors = [o for o in outcomes if isinstance(o, Exception)]
    assert not errors, f"concurrent fulfillment raised: {errors}"
