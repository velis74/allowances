"""Sliding-window wallets: available quota counts only consumption inside the window."""

from datetime import timedelta
from decimal import Decimal

from allowances.types import TransactionKind
from allowances.windows import utcnow
from tests.api import effective_balance, fulfill
from tests.conftest import open_wallet
from tests.helpers import transactions_for_wallet


async def test_a_wallet_without_a_reset_config_just_reports_its_stored_balance(store, alice):
    wallet = await open_wallet(store, alice, balance="42")

    assert await effective_balance(store, wallet) == Decimal("42")


async def test_a_rolling_window_ignores_consumption_that_has_aged_out(store, alice):
    now = utcnow()
    wallet = await open_wallet(store, alice, balance="100", reset_config={"window_seconds": 86400})
    await store.create_transaction(
        wallet_id=wallet.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-40"), at=now - timedelta(days=2)
    )
    await store.create_transaction(
        wallet_id=wallet.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-10"), at=now - timedelta(hours=1)
    )

    # Only the recent draw is still inside the 24h window.
    assert await effective_balance(store, wallet, at=now) == Decimal("90")


async def test_a_daily_window_drops_yesterdays_consumption(store, alice):
    now = utcnow().replace(hour=10, minute=0, second=0, microsecond=0)
    wallet = await open_wallet(store, alice, balance="200", reset_config={"time": "00:00:00"})
    await store.create_transaction(
        wallet_id=wallet.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-150"), at=now - timedelta(hours=20)
    )
    await store.create_transaction(
        wallet_id=wallet.id, kind=TransactionKind.CONSUMPTION, amount=Decimal("-30"), at=now - timedelta(hours=1)
    )

    assert await effective_balance(store, wallet, at=now) == Decimal("170")


async def test_fulfilling_from_a_windowed_wallet_never_moves_its_stored_balance(store, alice, provider):
    wallet = await open_wallet(store, alice, balance="100", reset_config={"window_seconds": 86400})

    result = await fulfill(store, "send_email", 30, {"device": alice}, provider)

    assert (await store.get_wallet(wallet.id)).balance == Decimal("100")  # the allotment is a constant
    assert result.satisfied is True
    assert len(await transactions_for_wallet(store, wallet.id)) == 1


async def test_a_second_call_in_the_same_window_sees_the_reduced_balance(store, alice, provider):
    await open_wallet(store, alice, balance="100", reset_config={"window_seconds": 86400})
    await fulfill(store, "send_email", 30, {"device": alice}, provider)

    second = await fulfill(store, "send_email", 80, {"device": alice}, provider, allow_partial=True)

    assert second.granted == Decimal("70")


async def test_the_quota_is_whole_again_once_the_window_has_passed(store, alice, provider):
    now = utcnow()
    await open_wallet(store, alice, balance="100", reset_config={"window_seconds": 3600})
    await fulfill(store, "send_email", 100, {"device": alice}, provider, at=now - timedelta(hours=2))

    result = await fulfill(store, "send_email", 100, {"device": alice}, provider, at=now)

    assert result.satisfied is True


async def test_a_windowed_wallet_is_never_archived_for_being_spent_out(store, alice, provider):
    wallet = await open_wallet(store, alice, balance="5", reset_config={"window_seconds": 86400})

    await fulfill(store, "send_email", 5, {"device": alice}, provider)

    assert await store.get_wallet(wallet.id) is not None


async def test_a_reversal_inside_the_window_restores_the_windowed_balance(store, alice, provider):
    from tests.api import unfulfill

    wallet = await open_wallet(store, alice, balance="100", reset_config={"window_seconds": 86400})
    result = await fulfill(store, "send_email", 30, {"device": alice}, provider)
    assert await effective_balance(store, await store.get_wallet(wallet.id)) == Decimal("70")

    await unfulfill(store, result.guid)

    # The storno falls inside the same window and offsets the consumption.
    assert await effective_balance(store, await store.get_wallet(wallet.id)) == Decimal("100")
    assert (await store.get_wallet(wallet.id)).balance == Decimal("100")
