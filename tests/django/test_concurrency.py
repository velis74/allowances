"""
The claim the rest of the library rests on: two callers spending the same wallet at the same
instant cannot both be granted the same balance.

This is the one property that cannot be proved by reading the code, because it is the database
enforcing it and each engine enforces it differently. Postgres locks the wallet rows for the
duration of the transaction. SQLite has no row lock at all and serialises whole transactions
instead. These tests run against whichever engine the suite is configured for, and pass on both —
that equivalence is the entire reason SQLite is supported rather than merely tolerated.

They need real committed transactions, hence ``transaction=True`` and the per-thread connection
teardown: threads get their own connection, and pytest-django only cleans up the main one.
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
    Fire ``count`` threads at ``work`` and release them together, returning what each one did.

    The barrier matters: without it the threads start far enough apart that the first finishes
    before the last begins, and the test passes for the wrong reason.
    """
    barrier = threading.Barrier(count)
    outcomes = [None] * count

    def worker(index):
        try:
            barrier.wait()
            outcomes[index] = work(index)
        except Exception as exc:  # recorded, not raised — the assertions below judge it
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

    # The wallet drained to exactly zero, and every unit that left it is on the ledger. A lost
    # update would show up as either a negative balance or fewer transactions than winners.
    assert Wallet.objects.get(pk=wallet.id).balance == Decimal("0")
    assert Transaction.objects.filter(wallet_id=wallet.id).count() == 5
    assert sum(t.amount for t in Transaction.objects.filter(wallet_id=wallet.id)) == Decimal("-10")


def test_concurrent_fulfill_across_two_wallets_does_not_double_draw(concurrent_provider):
    """
    The same guarantee where it is easiest to lose: a request that spans two wallets, so each
    caller holds one while it needs the other.
    """
    subject = SubjectRef("auth.user", 1)
    store = DjangoStore()
    # Five and seven rather than six and six: with an even split every request of three fits
    # inside one wallet and never spans the pair, which is the case being tested.
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

    # The invariant, rather than a transaction count: how many rows four grants take depends on
    # the order the winners happened to arrive in. What must hold is that the ledger accounts for
    # every unit that left, and that the two agree with what the callers were told they got.
    drawn = -sum(t.amount for t in Transaction.objects.filter(wallet_id__in=[first.id, second.id]))
    assert drawn == Decimal("12") - sum(balances.values())
    assert drawn == sum(o.granted for o in granted)
