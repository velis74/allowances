"""
Sliding-window and calendar-reset arithmetic.

A wallet with a ``reset_config`` doesn't get topped up by a background job — nothing ever writes
to it on a schedule. Instead its stored ``balance`` is a constant per-window allotment, and the
balance that governs decisions is that allotment plus every transaction inside the current window.
Old consumption ages out of the window on its own, which is what makes the quota appear to reset.

All arithmetic happens in UTC. The four supported shapes:

===================================================  ==========================================
``{"day_of_month": 1-31, "time": "HH:MM:SS"}``       monthly, clamped to the last real day
``{"day_of_week": 0-6, "time": "HH:MM:SS"}``         weekly, 0 = Monday (``date.weekday()``)
``{"time": "HH:MM:SS"}``                             daily
``{"window_seconds": <int>}``                        rolling window, no calendar anchor
===================================================  ==========================================
"""

from __future__ import annotations

import calendar

from datetime import datetime, timedelta, timezone as dt_timezone

DEFAULT_WINDOW_SECONDS = 86400


def utcnow() -> datetime:
    """The core's clock. Timezone-aware, always UTC."""
    return datetime.now(dt_timezone.utc)


def as_utc(at: datetime) -> datetime:
    """Naive datetimes are assumed to already be UTC rather than rejected."""
    if at.tzinfo is None:
        return at.replace(tzinfo=dt_timezone.utc)
    return at.astimezone(dt_timezone.utc)


def _parse_time_of_day(value: str) -> tuple[int, int, int]:
    hour, minute, second = (value.split(":") + ["0", "0"])[:3]
    return int(hour), int(minute), int(second)


def _last_day_clamped(year: int, month: int, day: int) -> int:
    """``day_of_month: 31`` in February means the 28th (or 29th), not an error."""
    return min(day, calendar.monthrange(year, month)[1])


def window_start(reset_config: dict, at: datetime) -> datetime:
    """The instant the window containing ``at`` began."""
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
