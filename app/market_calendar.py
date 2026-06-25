"""NYSE/NASDAQ trading-calendar helpers (trading days, early closes, structural days).

Uses pandas_market_calendars when available; falls back to a simple weekday rule
so the app still runs if the package is missing.
"""
from __future__ import annotations

from datetime import date, timedelta, time

from .timeutils import ET

try:  # pragma: no cover - exercised at runtime
    import pandas_market_calendars as mcal

    _CAL = mcal.get_calendar("NASDAQ")
except Exception:  # noqa: BLE001 - any import/runtime failure -> fallback
    _CAL = None


def is_trading_day(d: date) -> bool:
    if _CAL is None:
        return d.weekday() < 5
    try:
        sched = _CAL.schedule(start_date=d, end_date=d)
        return not sched.empty
    except Exception:  # noqa: BLE001
        return d.weekday() < 5


def is_early_close(d: date) -> bool:
    """True if the session closes before 16:00 ET (half-day)."""
    if _CAL is None:
        return False
    try:
        sched = _CAL.schedule(start_date=d, end_date=d)
        if sched.empty:
            return False
        close = sched.iloc[0]["market_close"].tz_convert(ET)
        return close.time() < time(16, 0)
    except Exception:  # noqa: BLE001
        return False


def prev_trading_day(d: date) -> date:
    cur = d - timedelta(days=1)
    for _ in range(10):
        if is_trading_day(cur):
            return cur
        cur -= timedelta(days=1)
    return cur


def next_trading_day(d: date) -> date:
    cur = d + timedelta(days=1)
    for _ in range(10):
        if is_trading_day(cur):
            return cur
        cur += timedelta(days=1)
    return cur


def _third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    # weekday(): Mon=0 .. Sun=6; Friday == 4
    offset = (4 - d.weekday()) % 7
    first_friday = d + timedelta(days=offset)
    return first_friday + timedelta(days=14)


def structural_flags(d: date) -> dict[str, bool]:
    """Detect structurally odd/low-quality session days."""
    third_fri = _third_friday(d.year, d.month)
    is_opex = d == third_fri
    is_quad_witching = is_opex and d.month in (3, 6, 9, 12)

    nxt = next_trading_day(d)
    is_day_before_holiday = (nxt - d).days > 1  # gap > weekend => holiday ahead
    prv = prev_trading_day(d)
    is_day_after_holiday = (d - prv).days > 3  # long gap behind

    is_month_end = nxt.month != d.month
    is_quarter_end = is_month_end and d.month in (3, 6, 9, 12)

    return {
        "early_close": is_early_close(d),
        "opex": is_opex,
        "quad_witching": is_quad_witching,
        "day_before_holiday": is_day_before_holiday,
        "day_after_holiday": is_day_after_holiday,
        "month_end": is_month_end,
        "quarter_end": is_quarter_end,
    }
