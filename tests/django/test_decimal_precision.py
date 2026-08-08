"""
The decimal range both engines agree on.

Postgres stores `DecimalField` exactly, to all twenty digits the column allows. SQLite stores it
as a float and Django rounds back to the field's six decimal places, which is exact to about
fifteen significant digits and lossy beyond — silently at first, then as `decimal.InvalidOperation`
once the rounded value no longer fits the column at all.

Nine integer digits plus six decimals is where the two engines still say the same thing, so that
is the range the library documents. This test is what stops that claim from drifting.
"""

from decimal import Decimal

import pytest

from allowances.engine import fulfill
from allowances.types import SubjectRef
from allowances_django.store import DjangoStore
from tests.conftest import CREDITS, provider_with

pytestmark = pytest.mark.django_db

#: The largest amount every supported engine represents exactly, and neighbours worth pinning.
EXACT = ["0.000001", "1.5", "999999999.999999", "123456789.123456", "0.123456"]


def open_wallet(balance):
    return DjangoStore().create_wallet(
        subject=SubjectRef("auth.user", 1),
        wallet_type="consumable",
        currency="credits",
        balance=Decimal(balance),
        overdraft_limit=Decimal("0"),
    )


@pytest.mark.parametrize("value", EXACT)
def test_balances_round_trip_exactly(value):
    wallet = open_wallet(value)

    assert DjangoStore().get_wallet(wallet.id).balance == Decimal(value)


def test_arithmetic_stays_exact_at_the_top_of_the_range():
    """Not just storage: a draw at the boundary leaves the exact remainder, not a float's guess."""
    wallet = open_wallet("999999999.999999")
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies=CREDITS,
    )

    store = DjangoStore()
    fulfill(store, "send_email", Decimal("0.000001"), {"device": SubjectRef("auth.user", 1)}, provider)

    assert store.get_wallet(wallet.id).balance == Decimal("999999999.999998")
