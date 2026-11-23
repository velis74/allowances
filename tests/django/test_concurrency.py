"""
Concurrent fulfilment: two callers spending the same wallet at the same time cannot both be
granted the same balance.

The database enforces this, and each engine enforces it differently. PostgreSQL locks the wallet
rows for the duration of the transaction. SQLite has no row locks and serialises whole
transactions. These tests run against the engine the suite is configured for and must pass on both,
once per store that keeps its data in the database (``store_factory``).

Each caller has a store of its own (``store_factory(index)``): its own worker thread for the synchronous
Django store, its own adorm session for the adorm stores; the asynchronous Django store runs each unit of
work on a thread of its own. They need committed transactions, hence ``transaction=True``.
"""

import asyncio

from decimal import Decimal

import pytest

from allowances.errors import InsufficientBalanceError
from allowances.types import SubjectRef
from allowances_django.models import Transaction, Wallet
from tests.api import fulfill
from tests.conftest import CREDITS, provider_with

WORKERS = 8

#: Seconds the callers of one test may take together before the test counts them as deadlocked.
DEADLOCK_TIMEOUT = 120


@pytest.fixture
def concurrent_provider():
    return provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies=CREDITS,
    )


async def run_concurrently(work, count=WORKERS):
    """
    Run the coroutine function ``work(index)`` for ``count`` callers released together; return each result or
    raised exception.

    Without the barrier the first caller can finish before the last starts, and the calls do not
    overlap.
    """
    barrier = asyncio.Barrier(count)

    async def caller(index):
        await barrier.wait()
        return await work(index)

    try:
        return await asyncio.wait_for(
            asyncio.gather(*(caller(i) for i in range(count)), return_exceptions=True), DEADLOCK_TIMEOUT
        )
    except TimeoutError:
        pytest.fail("a caller deadlocked")


async def ledger(wallet_ids):
    return [t async for t in Transaction.objects.filter(wallet_id__in=wallet_ids)]


async def test_concurrent_fulfill_grants_the_balance_exactly_once(store_factory, concurrent_provider):
    """Eight callers, two units each, ten in the wallet: five win, three are refused, none share."""
    subject = SubjectRef("auth.user", 1)
    # An explicit overdraft_limit of 0 keeps the wallet out of the drained-to-zero archive
    # trigger, so the test can still read its balance back. It permits no overdraft either way.
    wallet = await store_factory().create_wallet(
        subject=subject,
        wallet_type="consumable",
        currency="credits",
        balance=Decimal("10"),
        overdraft_limit=Decimal("0"),
    )
    stores = [store_factory(index) for index in range(WORKERS)]

    async def draw(index):
        try:
            return await fulfill(stores[index], "send_email", 2, {"device": subject}, concurrent_provider)
        except InsufficientBalanceError:
            return None

    outcomes = await run_concurrently(draw)

    unexpected = [o for o in outcomes if isinstance(o, Exception)]
    assert not unexpected, f"concurrent fulfillment raised: {unexpected}"

    granted = [o for o in outcomes if o is not None]
    assert len(granted) == 5
    assert all(o.satisfied for o in granted)

    # Balance exactly zero and one transaction per granted call; a lost update gives a negative
    # balance or fewer transactions.
    assert (await Wallet.objects.aget(pk=wallet.id)).balance == Decimal("0")
    assert await Transaction.objects.filter(wallet_id=wallet.id).acount() == 5
    assert sum(t.amount for t in await ledger([wallet.id])) == Decimal("-10")


async def test_concurrent_fulfill_across_two_wallets_does_not_double_draw(store_factory, concurrent_provider):
    """
    The same guarantee for requests that span two wallets, where each caller holds a lock on one
    while it needs the other.
    """
    subject = SubjectRef("auth.user", 1)
    store = store_factory()
    # Unequal balances: with six and six every request of three fits in one wallet and none
    # spans both.
    first, second = [
        await store.create_wallet(
            subject=subject,
            wallet_type="consumable",
            currency="credits",
            balance=Decimal(balance),
            overdraft_limit=Decimal("0"),
        )
        for balance in ("5", "7")
    ]
    stores = [store_factory(index) for index in range(WORKERS)]

    async def draw(index):
        try:
            return await fulfill(stores[index], "send_email", 3, {"device": subject}, concurrent_provider)
        except InsufficientBalanceError:
            return None

    outcomes = await run_concurrently(draw)

    unexpected = [o for o in outcomes if isinstance(o, Exception)]
    assert not unexpected, f"concurrent fulfillment raised: {unexpected}"

    granted = [o for o in outcomes if o is not None]
    assert len(granted) == 4  # 12 available, 3 each

    balances = {w.pk: w.balance async for w in Wallet.objects.filter(pk__in=[first.id, second.id])}
    assert len(balances) == 2
    assert all(balance >= 0 for balance in balances.values())

    # The row count depends on arrival order; the ledger sum must equal the balance drop and the
    # total granted.
    drawn = -sum(t.amount for t in await ledger([first.id, second.id]))
    assert drawn == Decimal("12") - sum(balances.values())
    assert drawn == sum(o.granted for o in granted)
