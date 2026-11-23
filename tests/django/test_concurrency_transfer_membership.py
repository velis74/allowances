"""
Concurrent transfers, transfers and reversals alongside fulfillment, and concurrent membership
changes: no deadlock, exact balances, and no membership cycle stored.
"""

from decimal import Decimal

import pytest

from allowances.engine import add_member, credit, fulfill, transfer, unfulfill
from allowances.errors import InsufficientBalanceError, MembershipCycleError
from allowances.types import SubjectRef, TransactionKind
from allowances_django.models import Membership, Transaction, TransactionArchive, Wallet
from allowances_django.store import DjangoStore
from tests.conftest import CREDITS, provider_with
from tests.django.test_concurrency import run_concurrently

pytestmark = pytest.mark.django_db(transaction=True)

ROUNDS = 20


def open_funded(subject, balance="1000"):
    return DjangoStore().create_wallet(
        subject=subject,
        wallet_type="consumable",
        currency="credits",
        balance=Decimal(balance),
        overdraft_limit=Decimal("0"),
    )


def balance(wallet):
    return Wallet.objects.get(pk=wallet.id).balance


def ledger_sum(wallet):
    return sum((t.amount for t in Transaction.objects.filter(wallet_id=wallet.id)), Decimal("0"))


def test_transfers_in_opposite_directions_do_not_deadlock():
    first = open_funded(SubjectRef("auth.user", 1), "100")
    second = open_funded(SubjectRef("auth.user", 2), "100")

    def move(index):
        source, target = (first, second) if index % 2 else (second, first)
        for _ in range(ROUNDS):
            transfer(DjangoStore(), source.id, target.id, Decimal("1"))

    outcomes = run_concurrently(move)

    errors = [o for o in outcomes if isinstance(o, Exception)]
    assert not errors, f"concurrent transfers raised: {errors}"
    assert balance(first) == Decimal("100")
    assert balance(second) == Decimal("100")
    assert ledger_sum(first) == ledger_sum(second) == Decimal("0")
    assert Transaction.objects.filter(kind=TransactionKind.TRANSFER).count() == 2 * 8 * ROUNDS


def test_fulfill_and_transfer_over_the_same_wallets_do_not_deadlock():
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
    user_wallet, team_wallet = open_funded(user), open_funded(team)

    def work(index):
        granted = Decimal("0")
        for _ in range(ROUNDS):
            kind = index % 4
            if kind == 0:
                granted += fulfill(DjangoStore(), "user_first", 3, {"user": user, "team": team}, provider).granted
            elif kind == 1:
                granted += fulfill(DjangoStore(), "team_first", 3, {"user": user, "team": team}, provider).granted
            else:
                source, target = (user_wallet, team_wallet) if kind == 2 else (team_wallet, user_wallet)
                try:
                    transfer(DjangoStore(), source.id, target.id, Decimal("2"))
                except InsufficientBalanceError:
                    pass
        return granted

    outcomes = run_concurrently(work)

    errors = [o for o in outcomes if isinstance(o, Exception)]
    assert not errors, f"concurrent fulfillment and transfer raised: {errors}"
    granted = sum(outcomes, Decimal("0"))
    assert granted == Decimal("3") * ROUNDS * 4
    assert balance(user_wallet) == Decimal("1000") + ledger_sum(user_wallet)
    assert balance(team_wallet) == Decimal("1000") + ledger_sum(team_wallet)
    assert balance(user_wallet) + balance(team_wallet) == Decimal("2000") - granted
    assert balance(user_wallet) >= 0 and balance(team_wallet) >= 0


def test_unfulfill_and_fulfill_over_the_same_wallets_do_not_deadlock():
    """Reversals lock the record, then both wallets; fulfillment walks them user first."""
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
    user_wallet, team_wallet = open_funded(user), open_funded(team, "0")
    reversers = 4
    guids = []
    for _ in range(reversers * ROUNDS):
        credit(DjangoStore(), team_wallet.id, Decimal("1"))
        guids.append(fulfill(DjangoStore(), "team_first", 3, {"user": user, "team": team}, provider).guid)
    assert balance(team_wallet) == Decimal("0")

    def work(index):
        granted = Decimal("0")
        for round_ in range(ROUNDS):
            if index % 2:
                unfulfill(DjangoStore(), guids[(index // 2) * ROUNDS + round_])
            else:
                granted += fulfill(DjangoStore(), "user_first", 3, {"user": user, "team": team}, provider).granted
        return granted

    outcomes = run_concurrently(work, count=2 * reversers)

    errors = [o for o in outcomes if isinstance(o, Exception)]
    assert not errors, f"concurrent fulfillment and reversal raised: {errors}"
    granted = sum(outcomes, Decimal("0"))
    assert granted == Decimal("3") * ROUNDS * reversers
    assert Transaction.objects.filter(kind=TransactionKind.STORNO).count() == 2 * reversers * ROUNDS
    assert balance(user_wallet) == Decimal("1000") + ledger_sum(user_wallet)
    assert balance(team_wallet) == ledger_sum(team_wallet)
    assert balance(user_wallet) + balance(team_wallet) == Decimal("1000") + reversers * ROUNDS - granted


def test_unfulfill_alongside_a_fulfill_that_archives_its_wallet_does_not_deadlock():
    provider = provider_with(
        features={
            "use": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"user"},
                "fulfillment_order": [("consumable", "user")],
            }
        },
        currencies=CREDITS,
    )
    for round_ in range(5):
        user = SubjectRef("auth.user", round_ + 1)
        wallet = DjangoStore().create_wallet(
            subject=user, wallet_type="consumable", currency="credits", balance=Decimal("40")
        )
        guids = [fulfill(DjangoStore(), "use", 1, {"user": user}, provider).guid for _ in range(16)]

        def work(index, user=user, guids=guids):
            if index % 2:
                result = fulfill(DjangoStore(), "use", 1000, {"user": user}, provider, allow_partial=True)
                return result.granted
            for guid in guids[index // 2 :: 4]:
                unfulfill(DjangoStore(), guid)
            return Decimal("0")

        outcomes = run_concurrently(work)

        errors = [o for o in outcomes if isinstance(o, Exception)]
        assert not errors, f"unfulfill alongside archival raised: {errors}"
        stornos = Transaction.objects.filter(wallet_id=wallet.id, kind=TransactionKind.STORNO).count()
        stornos += TransactionArchive.objects.filter(original_wallet_id=wallet.id, kind=TransactionKind.STORNO).count()
        live = Wallet.objects.filter(pk=wallet.id).first()
        left = live.balance if live is not None else Decimal("0")
        assert Decimal("40") - 16 + stornos == sum(outcomes, Decimal("0")) + left


def test_opposite_memberships_added_at_once_store_no_cycle():
    for pair in range(10):
        first, second = SubjectRef("auth.group", 2 * pair + 1), SubjectRef("auth.group", 2 * pair + 2)

        def join(index, first=first, second=second):
            member, group = (first, second) if index % 2 else (second, first)
            add_member(DjangoStore(), member, group)

        outcomes = run_concurrently(join, count=2)

        assert sum(o is None for o in outcomes) == 1, outcomes
        assert sum(isinstance(o, MembershipCycleError) for o in outcomes) == 1, outcomes
        stored = Membership.objects.filter(
            member_type="auth.group",
            member_id__in=[first.id, second.id],
            group_id__in=[first.id, second.id],
        )
        assert stored.count() == 1
