"""
Sliding-window and calendar-reset arithmetic.

A wallet with a ``reset_config`` is not refilled by a scheduled job. Its stored ``balance`` is the
per-window allotment; its effective balance is the allotment plus the sum of transactions inside the
current window. Transactions before the window start do not count.

All arithmetic is in UTC. The supported shapes:

===================================================  ==============================================
``{"day_of_month": 1-31, "time": "HH:MM:SS"}``       monthly; a day past the month's end means its
                                                     last day
``{"day_of_week": 0-6, "time": "HH:MM:SS"}``         weekly, 0 = Monday (``date.weekday()``)
``{"time": "HH:MM:SS"}``                             daily
``{"window_seconds": <int>}``                        rolling window, no calendar anchor
===================================================  ==============================================

``time`` may be ``HH``, ``HH:MM`` or ``HH:MM:SS``; it is optional for monthly and weekly configs
(default ``00:00:00``). A config with none of these keys is a rolling window of
``DEFAULT_WINDOW_SECONDS`` (86400 s).
"""

from __future__ import annotations

import calendar

from datetime import datetime, timedelta, timezone as dt_timezone

DEFAULT_WINDOW_SECONDS = 86400


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(dt_timezone.utc)


def as_utc(at: datetime) -> datetime:
    """Convert ``at`` to UTC; a naive datetime is taken as UTC."""
    if at.tzinfo is None:
        return at.replace(tzinfo=dt_timezone.utc)
    return at.astimezone(dt_timezone.utc)


def _parse_time_of_day(value: str) -> tuple[int, int, int]:
    hour, minute, second = (value.split(":") + ["0", "0"])[:3]
    return int(hour), int(minute), int(second)


def _last_day_clamped(year: int, month: int, day: int) -> int:
    """Clamp ``day`` to the length of the month (31 in February gives 28 or 29)."""
    return min(day, calendar.monthrange(year, month)[1])


def window_start(reset_config: dict, at: datetime) -> datetime:
    """Return the start of the window that contains ``at``."""
    at = as_utc(at)

    if "day_of_month" in reset_config:
        hour, minute, second = _parse_time_of_day(reset_config.get("time", "00:00:00"))
        day = reset_config["day_of_month"]
        year, month = at.year, at.month
        candidate = datetime(
            year, month, _last_day_clamped(year, month, day), hour, minute, second, tzinfo=dt_timezone.utc
        )
        if candidate > at:
            month -= 1
            if month == 0:
                month = 12
                year -= 1
            candidate = datetime(
                year, month, _last_day_clamped(year, month, day), hour, minute, second, tzinfo=dt_timezone.utc
            )
        return candidate

    if "day_of_week" in reset_config:
        hour, minute, second = _parse_time_of_day(reset_config.get("time", "00:00:00"))
        days_diff = (at.weekday() - reset_config["day_of_week"]) % 7
        candidate = (at - timedelta(days=days_diff)).replace(hour=hour, minute=minute, second=second, microsecond=0)
        if candidate > at:
            candidate -= timedelta(days=7)
        return candidate

    if "time" in reset_config:
        hour, minute, second = _parse_time_of_day(reset_config["time"])
        candidate = at.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if candidate > at:
            candidate -= timedelta(days=1)
        return candidate

    return at - timedelta(seconds=reset_config.get("window_seconds", DEFAULT_WINDOW_SECONDS))
