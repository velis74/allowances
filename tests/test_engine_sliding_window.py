from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest

from django.contrib.auth.models import User
from django.utils import timezone

from django_licensing.engine import effective_balance, fulfill, window_start
from django_licensing.models import Transaction, TransactionKind, Wallet, WalletType
from django_licensing.providers import SettingsProvider

pytestmark = pytest.mark.django_db


@pytest.fixture
def alice():
    return User.objects.create(username="alice")


def backdate(txn, when):
    Transaction.objects.filter(pk=txn.pk).update(created_at=when)


# -- pure window_start() tests, no DB -----------------------------------------------------------


def test_window_start_rolling_default_is_24h():
    at = datetime(2026, 8, 6, 12, 0, 0, tzinfo=dt_timezone.utc)

    start = window_start({}, at)

    assert start == at - timedelta(hours=24)


def test_window_start_rolling_explicit_window_seconds():
    at = datetime(2026, 8, 6, 12, 0, 0, tzinfo=dt_timezone.utc)

    start = window_start({"window_seconds": 3600}, at)

    assert start == at - timedelta(hours=1)


def test_window_start_daily():
    at = datetime(2026, 8, 6, 12, 30, 0, tzinfo=dt_timezone.utc)

    start = window_start({"time": "00:00:00"}, at)

    assert start == datetime(2026, 8, 6, 0, 0, 0, tzinfo=dt_timezone.utc)


def test_window_start_daily_before_reset_time_uses_previous_day():
    at = datetime(2026, 8, 6, 3, 0, 0, tzinfo=dt_timezone.utc)

    start = window_start({"time": "06:00:00"}, at)

    assert start == datetime(2026, 8, 5, 6, 0, 0, tzinfo=dt_timezone.utc)


def test_window_start_weekly():
    # 2026-08-06 is a Thursday (weekday()==3). Reset on Monday (0) at midnight.
    at = datetime(2026, 8, 6, 12, 0, 0, tzinfo=dt_timezone.utc)

    start = window_start({"day_of_week": 0, "time": "00:00:00"}, at)

    assert start == datetime(2026, 8, 3, 0, 0, 0, tzinfo=dt_timezone.utc)


def test_window_start_weekly_before_reset_uses_previous_week():
    # Monday 2026-08-03, before the weekly reset time of noon.
    at = datetime(2026, 8, 3, 6, 0, 0, tzinfo=dt_timezone.utc)

    start = window_start({"day_of_week": 0, "time": "12:00:00"}, at)

    assert start == datetime(2026, 7, 27, 12, 0, 0, tzinfo=dt_timezone.utc)


def test_window_start_monthly():
    at = datetime(2026, 8, 15, 12, 0, 0, tzinfo=dt_timezone.utc)

    start = window_start({"day_of_month": 1, "time": "00:00:00"}, at)

    assert start == datetime(2026, 8, 1, 0, 0, 0, tzinfo=dt_timezone.utc)


def test_window_start_monthly_before_reset_day_uses_previous_month():
    at = datetime(2026, 8, 3, 12, 0, 0, tzinfo=dt_timezone.utc)

    start = window_start({"day_of_month": 15, "time": "00:00:00"}, at)

    assert start == datetime(2026, 7, 15, 0, 0, 0, tzinfo=dt_timezone.utc)


def test_window_start_monthly_clamps_to_last_day_of_shorter_month():
    at = datetime(2026, 3, 5, 12, 0, 0, tzinfo=dt_timezone.utc)

    # day_of_month=31 doesn't exist in February — clamp to Feb 28 (2026 is not a leap year).
    start = window_start({"day_of_month": 31, "time": "00:00:00"}, at)

    assert start == datetime(2026, 2, 28, 0, 0, 0, tzinfo=dt_timezone.utc)


# -- effective_balance() against real transactions -----------------------------------------------


def test_effective_balance_non_sliding_window_wallet_returns_stored_balance(alice):
    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("42")
    )

    assert effective_balance(wallet) == Decimal("42")


def test_effective_balance_rolling_window_excludes_old_transactions(alice):
    now = timezone.now()
    wallet = Wallet.objects.create(
        subject=alice,
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        balance=Decimal("100"),
        reset_config={"window_seconds": 86400},
    )
    old_txn = Transaction.objects.create(wallet=wallet, kind=TransactionKind.CONSUMPTION, amount=Decimal("-40"))
    backdate(old_txn, now - timedelta(days=2))
    recent_txn = Transaction.objects.create(wallet=wallet, kind=TransactionKind.CONSUMPTION, amount=Decimal("-10"))
    backdate(recent_txn, now - timedelta(hours=1))

    balance = effective_balance(wallet, at=now)

    assert balance == Decimal("90")  # 100 allotment - 10 (only the recent transaction counts)


def test_effective_balance_daily_window_resets_at_midnight(alice):
    now = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0)
    wallet = Wallet.objects.create(
        subject=alice,
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        balance=Decimal("200"),
        reset_config={"time": "00:00:00"},
    )
    yesterday_txn = Transaction.objects.create(wallet=wallet, kind=TransactionKind.CONSUMPTION, amount=Decimal("-150"))
    backdate(yesterday_txn, now - timedelta(hours=20))
    today_txn = Transaction.objects.create(wallet=wallet, kind=TransactionKind.CONSUMPTION, amount=Decimal("-30"))
    backdate(today_txn, now - timedelta(hours=1))

    balance = effective_balance(wallet, at=now)

    assert balance == Decimal("170")  # 200 - 30, yesterday's consumption already aged out


def test_fulfill_on_sliding_window_wallet_never_decrements_stored_balance(alice):
    wallet = Wallet.objects.create(
        subject=alice,
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        balance=Decimal("100"),
        reset_config={"window_seconds": 86400},
    )
    provider = SettingsProvider(
        config={
            "features": {
                "send_email": {
                    "consumable": {"currencies": ["credits"]},
                    "subjects": {"device"},
                    "fulfillment_order": [("consumable", "device")],
                }
            },
            "currencies": {"credits": {"general": True, "round_fn": None}},
        }
    )

    result = fulfill("send_email", 30, {"device": alice}, provider=provider)

    wallet.refresh_from_db()
    assert wallet.balance == Decimal("100")  # allotment untouched
    assert result.satisfied is True
    assert Transaction.objects.filter(wallet=wallet).count() == 1

    # A second, later fulfill within the same window sees the reduced effective balance.
    second = fulfill("send_email", 80, {"device": alice}, provider=provider, allow_partial=True)
    assert second.granted == Decimal("70")  # 100 - 30 already consumed this window
