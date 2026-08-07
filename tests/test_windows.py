"""Window arithmetic. Pure functions, no store, no database."""

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest

from allowances.rounding import apply_round
from allowances.windows import window_start


def test_rolling_window_defaults_to_24h():
    at = datetime(2026, 8, 6, 12, 0, 0, tzinfo=dt_timezone.utc)

    assert window_start({}, at) == at - timedelta(hours=24)


def test_rolling_window_honours_explicit_window_seconds():
    at = datetime(2026, 8, 6, 12, 0, 0, tzinfo=dt_timezone.utc)

    assert window_start({"window_seconds": 3600}, at) == at - timedelta(hours=1)


def test_daily_window_starts_at_todays_reset_time():
    at = datetime(2026, 8, 6, 12, 30, 0, tzinfo=dt_timezone.utc)

    assert window_start({"time": "00:00:00"}, at) == datetime(2026, 8, 6, 0, 0, 0, tzinfo=dt_timezone.utc)


def test_daily_window_before_reset_time_belongs_to_yesterday():
    at = datetime(2026, 8, 6, 3, 0, 0, tzinfo=dt_timezone.utc)

    assert window_start({"time": "06:00:00"}, at) == datetime(2026, 8, 5, 6, 0, 0, tzinfo=dt_timezone.utc)


def test_weekly_window_starts_on_the_configured_weekday():
    # 2026-08-06 is a Thursday; reset on Monday.
    at = datetime(2026, 8, 6, 12, 0, 0, tzinfo=dt_timezone.utc)

    assert window_start({"day_of_week": 0, "time": "00:00:00"}, at) == datetime(
        2026, 8, 3, 0, 0, 0, tzinfo=dt_timezone.utc
    )


def test_weekly_window_before_reset_belongs_to_previous_week():
    at = datetime(2026, 8, 3, 6, 0, 0, tzinfo=dt_timezone.utc)  # Monday, before the noon reset

    assert window_start({"day_of_week": 0, "time": "12:00:00"}, at) == datetime(
        2026, 7, 27, 12, 0, 0, tzinfo=dt_timezone.utc
    )


def test_monthly_window_starts_on_the_configured_day():
    at = datetime(2026, 8, 15, 12, 0, 0, tzinfo=dt_timezone.utc)

    assert window_start({"day_of_month": 1, "time": "00:00:00"}, at) == datetime(
        2026, 8, 1, 0, 0, 0, tzinfo=dt_timezone.utc
    )


def test_monthly_window_before_reset_day_belongs_to_previous_month():
    at = datetime(2026, 8, 3, 12, 0, 0, tzinfo=dt_timezone.utc)

    assert window_start({"day_of_month": 15, "time": "00:00:00"}, at) == datetime(
        2026, 7, 15, 0, 0, 0, tzinfo=dt_timezone.utc
    )


def test_monthly_window_clamps_to_the_last_day_of_a_shorter_month():
    at = datetime(2026, 3, 5, 12, 0, 0, tzinfo=dt_timezone.utc)

    # There is no 31st of February — the window starts on the 28th (2026 is not a leap year).
    assert window_start({"day_of_month": 31, "time": "00:00:00"}, at) == datetime(
        2026, 2, 28, 0, 0, 0, tzinfo=dt_timezone.utc
    )


def test_naive_datetimes_are_treated_as_utc():
    naive = datetime(2026, 8, 6, 12, 0, 0)

    assert window_start({"window_seconds": 60}, naive) == datetime(2026, 8, 6, 11, 59, 0, tzinfo=dt_timezone.utc)


@pytest.mark.parametrize(
    "value,round_fn,expected",
    [
        ("2.7", None, "2.7"),
        ("2.7", "floor", "2"),
        ("2.1", "ceil", "3"),
        ("2.5", "round", "3"),
        ("2.4", "round", "2"),
    ],
)
def test_apply_round(value, round_fn, expected):
    assert apply_round(Decimal(value), round_fn) == Decimal(expected)


def test_apply_round_rejects_an_unknown_mode():
    with pytest.raises(ValueError, match="Unknown round_fn"):
        apply_round(Decimal("1"), "nearest-tuesday")
