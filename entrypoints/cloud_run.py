"""Cloud Run request helpers for InteractiveBrokersPlatform."""

from __future__ import annotations

from datetime import datetime
from importlib import import_module

import pytz


def _load_market_calendar(calendar_name: str, *, logger) -> object | None:
    try:
        module = import_module("pandas_market_calendars")
        return module.get_calendar(calendar_name)
    except Exception as exc:
        logger(
            f"pandas_market_calendars unavailable for {calendar_name}: {exc}; "
            "failing closed as market closed"
        )
        return None


def is_market_open_now(
    *,
    calendar_name="NYSE",
    timezone_name="America/New_York",
    logger=lambda _message: None,
):
    """Return whether the US equity regular session is open right now."""
    tz_ny = pytz.timezone(timezone_name)
    now_ny = datetime.now(tz_ny)
    calendar = _load_market_calendar(calendar_name, logger=logger)
    if calendar is None:
        return False
    schedule = calendar.schedule(start_date=now_ny.date(), end_date=now_ny.date())
    if len(getattr(schedule, "index", ())) == 0:
        return False
    try:
        return calendar.open_at_time(schedule, now_ny)
    except ValueError as exc:
        if "not covered by the schedule" not in str(exc):
            raise
        return False


def is_market_open_today(
    *,
    calendar_name="NYSE",
    timezone_name="America/New_York",
    logger=lambda _message: None,
) -> bool:
    """Return whether today is a US equity trading session (legacy helper)."""
    tz_ny = pytz.timezone(timezone_name)
    now_ny = datetime.now(tz_ny)
    calendar = _load_market_calendar(calendar_name, logger=logger)
    if calendar is None:
        return False
    schedule = calendar.schedule(start_date=now_ny.date(), end_date=now_ny.date())
    return len(getattr(schedule, "index", ())) > 0
