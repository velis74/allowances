"""
Operations that lock several wallets take the locks in one global order, so two of them can never
wait for each other in a cycle.
"""

from decimal import Decimal

import pytest

from allowances.engine import fulfill
from allowances.errors import InsufficientBalanceError
from allowances.types import SubjectRef
from allowances_django.store import DjangoStore
from tests.conftest import CREDITS, provider_with
from tests.django.test_concurrency import run_concurrently

pytestmark = pytest.mark.django_db(transaction=True)


def test_fulfill_with_opposite_orders_over_the_same_wallets_does_not_deadlock():
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
    store = DjangoStore()
    for subject in (user, team):
        store.create_wallet(
            subject=subject,
            wallet_type="consumable",
            currency="credits",
            balance=Decimal("1000"),
            overdraft_limit=Decimal("0"),
        )

    def draw(index):
        feature = "user_first" if index % 2 else "team_first"
        outcomes = []
        for _ in range(20):
            try:
                outcomes.append(fulfill(DjangoStore(), feature, 3, {"user": user, "team": team}, provider))
            except InsufficientBalanceError:
                outcomes.append(None)
        return outcomes

    outcomes = run_concurrently(draw)

    errors = [o for o in outcomes if isinstance(o, Exception)]
    assert not errors, f"concurrent fulfillment raised: {errors}"
