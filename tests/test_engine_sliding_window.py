"""Sliding-window wallets: quota that appears to reset because old consumption ages out."""

from datetime import timedelta
from decimal import Decimal

from allowances.engine import effective_balance, fulfill
from allowances.types import TransactionKind
from allowances.windows import utcnow
from tests.conftest import open_wallet
from tests.helpers import transactions_for_wallet


def test_a_wallet_without_a_reset_config_just_reports_its_stored_balance(store, alice):
    wallet = open_wallet(store, alice, balance="42")

    assert effective_balance(store, wallet) == Decimal("42")


def test_a_rolling_window_ignores_consumption_that_has_aged_out(store, alice):
    now = utcnow()
    wallet = open_wallet(store, alice, balance="100", reset_config={"window_seconds": 86400})
    store.create_transaction(
        wallet_id=wallet.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-40"), at=now - timedelta(days=2)
    )
    store.create_transaction(
        wallet_id=wallet.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-10"), at=now - timedelta(hours=1)
    )

    # Only the recent draw is still inside the 24h window.
    assert effective_balance(store, wallet, at=now) == Decimal("90")


def test_a_daily_window_drops_yesterdays_consumption(store, alice):
    now = utcnow().replace(hour=10, minute=0, second=0, microsecond=0)
    wallet = open_wallet(store, alice, balance="200", reset_config={"time": "00:00:00"})
    store.create_transaction(
        wallet_id=wallet.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-150"), at=now - timedelta(hours=20)
    )
    store.create_transaction(
        wallet_id=wallet.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-30"), at=now - timedelta(hours=1)
    )

    assert effective_balance(store, wallet, at=now) == Decimal("170")


def test_fulfilling_from_a_windowed_wallet_never_moves_its_stored_balance(store, alice, provider):
    wallet = open_wallet(store, alice, balance="100", reset_config={"window_seconds": 86400})

    result = fulfill(store, "send_email", 30, {"device": alice}, provider)

    assert store.get_wallet(wallet.id).balance == Decimal("100")  # the allotment is a constant
    assert result.satisfied is True
    assert len(transactions_for_wallet(store, wallet.id)) == 1


def test_a_second_call_in_the_same_window_sees_the_reduced_balance(store, alice, provider):
    open_wallet(store, alice, balance="100", reset_config={"window_seconds": 86400})
    fulfill(store, "send_email", 30, {"device": alice}, provider)

    second = fulfill(store, "send_email", 80, {"device": alice}, provider, allow_partial=True)

    assert second.granted == Decimal("70")


def test_the_quota_is_whole_again_once_the_window_has_passed(store, alice, provider):
    now = utcnow()
    open_wallet(store, alice, balance="100", reset_config={"window_seconds": 3600})
    fulfill(store, "send_email", 100, {"device": alice}, provider, at=now - timedelta(hours=2))

    result = fulfill(store, "send_email", 100, {"device": alice}, provider, at=now)

    assert result.satisfied is True


def test_a_windowed_wallet_is_never_archived_for_being_spent_out(store, alice, provider):
    wallet = open_wallet(store, alice, balance="5", reset_config={"window_seconds": 86400})

    fulfill(store, "send_email", 5, {"device": alice}, provider)

    assert store.get_wallet(wallet.id) is not None


def test_a_reversal_inside_the_window_restores_the_windowed_balance(store, alice, provider):
    from allowances.engine import unfulfill

    wallet = open_wallet(store, alice, balance="100", reset_config={"window_seconds": 86400})
    result = fulfill(store, "send_email", 30, {"device": alice}, provider)
    assert effective_balance(store, store.get_wallet(wallet.id)) == Decimal("70")

    unfulfill(store, result.guid)

    # The storno lands inside the same window and nets the consumption back out.
    assert effective_balance(store, store.get_wallet(wallet.id)) == Decimal("100")
    assert store.get_wallet(wallet.id).balance == Decimal("100")
