"""
Concurrent fulfilment: two callers spending the same wallet at the same time cannot both be
granted the same balance.

The database enforces this, and each engine enforces it differently. PostgreSQL locks the wallet
rows for the duration of the transaction. SQLite has no row locks and serialises whole
transactions. These tests run against the engine the suite is configured for and must pass on both.

They need committed transactions, hence ``transaction=True`` and the per-thread connection
teardown: each thread has its own connection, and pytest-django closes only the main one.
"""

import threading

from decimal import Decimal

import pytest

from django.db import connections

from allowances.engine import fulfill
from allowances.errors import InsufficientBalanceError
from allowances.types import SubjectRef
from allowances_django.models import Transaction, Wallet
from allowances_django.store import DjangoStore
from tests.conftest import CREDITS, provider_with

WORKERS = 8

pytestmark = pytest.mark.django_db(transaction=True)


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


def run_concurrently(work, count=WORKERS):
    """
    Run ``work`` in ``count`` threads released together; return each result or raised exception.

    Without the barrier the first thread can finish before the last starts, and the calls do not
    overlap.
    """
    barrier = threading.Barrier(count)
    outcomes = [None] * count

    def worker(index):
        try:
            barrier.wait()
            outcomes[index] = work(index)
        except Exception as exc:  # returned, not raised
            outcomes[index] = exc
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads), "a worker deadlocked"
    return outcomes


def test_concurrent_fulfill_grants_the_balance_exactly_once(concurrent_provider):
    """Eight callers, two units each, ten in the wallet: five win, three are refused, none share."""
    subject = SubjectRef("auth.user", 1)
    # An explicit overdraft_limit of 0 keeps the wallet out of the drained-to-zero archive
    # trigger, so the test can still read its balance back. It permits no overdraft either way.
    wallet = DjangoStore().create_wallet(
        subject=subject,
        wallet_type="consumable",
        currency="credits",
        balance=Decimal("10"),
        overdraft_limit=Decimal("0"),
    )

    def draw(_index):
        try:
            return fulfill(DjangoStore(), "send_email", 2, {"device": subject}, concurrent_provider)
        except InsufficientBalanceError:
            return None

    outcomes = run_concurrently(draw)

    unexpected = [o for o in outcomes if isinstance(o, Exception)]
    assert not unexpected, f"concurrent fulfillment raised: {unexpected}"

    granted = [o for o in outcomes if o is not None]
    assert len(granted) == 5
    assert all(o.satisfied for o in granted)

    # Balance exactly zero and one transaction per granted call; a lost update gives a negative
    # balance or fewer transactions.
    assert Wallet.objects.get(pk=wallet.id).balance == Decimal("0")
    assert Transaction.objects.filter(wallet_id=wallet.id).count() == 5
    assert sum(t.amount for t in Transaction.objects.filter(wallet_id=wallet.id)) == Decimal("-10")


def test_concurrent_fulfill_across_two_wallets_does_not_double_draw(concurrent_provider):
    """
    The same guarantee for requests that span two wallets, where each caller holds a lock on one
    while it needs the other.
    """
    subject = SubjectRef("auth.user", 1)
    store = DjangoStore()
    # Unequal balances: with six and six every request of three fits in one wallet and none
    # spans both.
    first, second = (
        store.create_wallet(
            subject=subject,
            wallet_type="consumable",
            currency="credits",
            balance=Decimal(balance),
            overdraft_limit=Decimal("0"),
        )
        for balance in ("5", "7")
    )

    def draw(_index):
        try:
            return fulfill(DjangoStore(), "send_email", 3, {"device": subject}, concurrent_provider)
        except InsufficientBalanceError:
            return None

    outcomes = run_concurrently(draw)

    unexpected = [o for o in outcomes if isinstance(o, Exception)]
    assert not unexpected, f"concurrent fulfillment raised: {unexpected}"

    granted = [o for o in outcomes if o is not None]
    assert len(granted) == 4  # 12 available, 3 each

    balances = {w.pk: w.balance for w in Wallet.objects.filter(pk__in=[first.id, second.id])}
    assert len(balances) == 2
    assert all(balance >= 0 for balance in balances.values())

    # The row count depends on arrival order; the ledger sum must equal the balance drop and the
    # total granted.
    drawn = -sum(t.amount for t in Transaction.objects.filter(wallet_id__in=[first.id, second.id]))
    assert drawn == Decimal("12") - sum(balances.values())
    assert drawn == sum(o.granted for o in granted)
