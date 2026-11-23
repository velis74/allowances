"""Rate-0 rights, ``close_wallet`` and the actor recorded on ledger entries."""

from datetime import timedelta
from decimal import Decimal

import pytest

from allowances import engine
from allowances.engine import (
    archive_expired_wallets,
    close_wallet,
    consumed,
    credit,
    fulfill,
    peek,
    plan_switch,
    unfulfill,
)
from allowances.errors import InsufficientBalanceError, WalletNotFoundError
from allowances.types import SubjectRef, SwitchPolicy, TransactionKind, WalletSpec, WalletType
from allowances.windows import utcnow
from tests.conftest import CREDITS, open_wallet, provider_with
from tests.helpers import add_modifier, archived_wallet_ids, transaction_count, transactions_for_wallet

RIGHT = "edit_right"


@pytest.fixture
def rights():
    return provider_with(
        features={
            "edit": {
                "grant": {"currencies": [(RIGHT, 0)]},
                "consumable": {"currencies": ["credits"]},
                "subjects": {"user", "team"},
                "fulfillment_order": [("grant", None), ("consumable", None)],
                "overdraft": True,
            }
        },
        currencies={**CREDITS, RIGHT: {}},
    )


def _right(store, subject, balance="1", **kwargs):
    return open_wallet(store, subject, currency=RIGHT, balance=balance, wallet_type=WalletType.GRANT, **kwargs)


def test_peek_with_a_held_right_is_satisfied_and_writes_nothing(store, alice, rights):
    _right(store, alice)

    result = peek(store, "edit", 5, {"user": alice}, rights)

    assert result.satisfied is True
    assert result.granted == Decimal("5")
    assert transaction_count(store) == 0


def test_fulfill_with_a_right_records_a_zero_consumption_and_keeps_the_balance(store, alice, rights):
    wallet = _right(store, alice)

    result = fulfill(store, "edit", 5, {"user": alice}, rights)

    assert result.satisfied is True
    assert [(t.wallet_id, t.kind, t.amount) for t in result.transactions] == [
        (wallet.id, TransactionKind.CONSUMPTION, Decimal("0"))
    ]
    assert result.transactions[0].actor == alice
    assert store.get_wallet(wallet.id).balance == Decimal("1")
    assert consumed(store, wallet.id, utcnow() - timedelta(days=1), utcnow() + timedelta(days=1)) == Decimal("0")


def test_a_right_with_a_zero_balance_is_not_held(store, alice, rights):
    _right(store, alice, balance="0", overdraft_limit=Decimal("10"))

    assert peek(store, "edit", 1, {"user": alice}, rights).satisfied is False
    with pytest.raises(InsufficientBalanceError):
        fulfill(store, "edit", 1, {"user": alice}, rights)


def test_an_expired_right_is_not_held(store, alice, rights):
    _right(store, alice, valid_until=utcnow() - timedelta(seconds=1))

    assert peek(store, "edit", 1, {"user": alice}, rights).satisfied is False


def test_a_right_covers_what_an_earlier_wallet_left_uncovered(store, alice, rights):
    provider = provider_with(
        features={
            "edit": {
                "grant": {"currencies": [(RIGHT, 0)]},
                "consumable": {"currencies": ["credits"]},
                "subjects": {"user"},
                "fulfillment_order": [("consumable", "user"), ("grant", "user")],
            }
        },
        currencies={**CREDITS, RIGHT: {}},
    )
    credits = open_wallet(store, alice, balance="2", overdraft_limit=Decimal("0"))
    right = _right(store, alice)

    result = fulfill(store, "edit", 5, {"user": alice}, provider)

    assert [(t.wallet_id, t.amount) for t in result.transactions] == [
        (credits.id, Decimal("-2")),
        (right.id, Decimal("0")),
    ]
    assert result.granted == Decimal("5")


def test_a_modifier_of_zero_makes_a_paid_wallet_cover_the_request_for_free(store, alice, rights):
    wallet = open_wallet(store, alice, balance="1")
    add_modifier(store, subject=alice, feature="edit", factor="0")

    result = fulfill(store, "edit", 50, {"user": alice}, rights)

    assert result.satisfied is True
    assert [t.amount for t in result.transactions] == [Decimal("0")]
    assert store.get_wallet(wallet.id).balance == Decimal("1")


def test_unfulfilling_a_use_of_a_right_writes_a_zero_storno_with_the_reversing_actor(store, alice, bob, rights):
    wallet = _right(store, alice)
    result = fulfill(store, "edit", 1, {"user": alice}, rights)

    unfulfill(store, result.guid, actor=bob)

    stornos = [t for t in transactions_for_wallet(store, wallet.id) if t.kind == TransactionKind.STORNO]
    assert [(t.amount, t.actor, t.storno_of_id) for t in stornos] == [(Decimal("0"), bob, result.transactions[0].id)]
    assert store.get_wallet(wallet.id).balance == Decimal("1")


# -- actor -------------------------------------------------------------------------------------------


def test_fulfill_with_several_subjects_records_no_actor_by_default(store, alice, bob, rights):
    open_wallet(store, bob, balance="10")

    result = fulfill(store, "edit", 1, {"user": alice, "team": bob}, rights)

    assert [t.actor for t in result.transactions] == [None]


def test_fulfill_records_an_explicit_actor(store, alice, bob, rights):
    open_wallet(store, bob, balance="10")

    result = fulfill(store, "edit", 1, {"user": alice, "team": bob}, rights, actor=alice)

    assert [t.actor for t in result.transactions] == [alice]


def test_credit_records_its_actor(store, alice, bob):
    wallet = open_wallet(store, alice, balance="0")

    txn = credit(store, wallet.id, "5", actor=bob)

    assert txn.actor == bob
    assert transactions_for_wallet(store, wallet.id)[0].actor == bob


# -- close_wallet ------------------------------------------------------------------------------------


def test_close_wallet_revokes_a_right_without_a_ledger_entry(store, alice, rights):
    wallet = _right(store, alice)
    at = utcnow()

    closed = close_wallet(store, wallet.id, at=at)

    assert closed.valid_until == at
    assert store.get_wallet(wallet.id).valid_until == at
    assert store.get_wallet(wallet.id).balance == Decimal("1")
    assert transaction_count(store) == 0
    assert peek(store, "edit", 1, {"user": alice}, rights, at=at + timedelta(seconds=1)).satisfied is False


def test_close_wallet_keeps_an_earlier_end(store, alice):
    ends = utcnow() + timedelta(hours=1)
    wallet = _right(store, alice, valid_until=ends)

    closed = close_wallet(store, wallet.id, at=ends + timedelta(days=1))

    assert closed.valid_until == ends


def test_a_closed_wallet_is_archived_later(store, alice):
    wallet = _right(store, alice)
    at = utcnow()
    close_wallet(store, wallet.id, at=at)

    assert archive_expired_wallets(store, at=at + timedelta(seconds=1)) == 1
    assert archived_wallet_ids(store) == [wallet.id]


def test_close_wallet_on_an_unknown_wallet_raises(store):
    with pytest.raises(WalletNotFoundError):
        close_wallet(store, 987654)


def test_close_wallet_drops_a_scheduled_switch_that_is_not_yet_effective(store, alice, monkeypatch):
    now = utcnow()
    monkeypatch.setattr(engine, "utcnow", lambda: now)
    spec = WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("100"), on_switch=SwitchPolicy.TOP_UP)
    [wallet] = plan_switch(store, {"user": alice}, [spec])
    plan_switch(
        store,
        {"user": alice},
        [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("200"), on_switch=SwitchPolicy.TOP_UP)],
        effective_date=now + timedelta(days=10),
    )
    assert store.get_wallet(wallet.id).scheduled is not None

    closed = close_wallet(store, wallet.id, at=now)

    assert closed.scheduled is None
    assert closed.valid_until == now
    assert store.get_wallet(wallet.id).balance == Decimal("100")


def test_close_wallet_applies_a_scheduled_adjustment_already_due(store, alice, monkeypatch):
    now = utcnow()
    monkeypatch.setattr(engine, "utcnow", lambda: now)
    spec = WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("100"), on_switch=SwitchPolicy.TOP_UP)
    [wallet] = plan_switch(store, {"user": alice}, [spec])
    plan_switch(
        store,
        {"user": alice},
        [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("150"), on_switch=SwitchPolicy.TOP_UP)],
        effective_date=now + timedelta(days=1),
    )

    closed = close_wallet(store, wallet.id, at=now + timedelta(days=2))

    assert closed.balance == Decimal("150")
    assert closed.scheduled is None


def test_actor_subject_ref_round_trips(store):
    """Actors of any subject type are stored as given."""
    wallet = open_wallet(store, SubjectRef("org", "acme"), balance="0")
    actor = SubjectRef("device", "x-1")

    credit(store, wallet.id, "1", actor=actor)

    assert transactions_for_wallet(store, wallet.id)[0].actor == actor
