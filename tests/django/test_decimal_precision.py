"""
The decimal range both engines store exactly.

Postgres stores `DecimalField` exactly, to all twenty digits the column allows. SQLite stores it
as a float and Django rounds it back to the field's six decimal places, which is exact to about
fifteen significant digits. Larger values lose precision, or raise `decimal.InvalidOperation` when
the rounded value exceeds the column.

Both engines store nine integer digits plus six decimals exactly; this is the range the library
documents. The tests run once per store that keeps its data in the database (``db_store``).
"""

from decimal import Decimal

import pytest

from allowances.types import SubjectRef
from tests.api import fulfill
from tests.conftest import CREDITS, provider_with

#: Values every supported engine stores exactly, including the largest (999999999.999999).
EXACT = ["0.000001", "1.5", "999999999.999999", "123456789.123456", "0.123456"]


async def open_wallet(store, balance):
    return await store.create_wallet(
        subject=SubjectRef("auth.user", 1),
        wallet_type="consumable",
        currency="credits",
        balance=Decimal(balance),
        overdraft_limit=Decimal("0"),
    )


@pytest.mark.parametrize("value", EXACT)
async def test_balances_round_trip_exactly(db_store, value):
    wallet = await open_wallet(db_store, value)

    assert (await db_store.get_wallet(wallet.id)).balance == Decimal(value)


async def test_arithmetic_stays_exact_at_the_top_of_the_range(db_store):
    """A draw at the top of the range leaves the exact decimal remainder."""
    store = db_store
    wallet = await open_wallet(store, "999999999.999999")
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

    await fulfill(store, "send_email", Decimal("0.000001"), {"device": SubjectRef("auth.user", 1)}, provider)

    assert (await store.get_wallet(wallet.id)).balance == Decimal("999999999.999998")
