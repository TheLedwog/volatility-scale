"""Timezone + session-window helpers. Everything is anchored to America/New_York."""
from __future__ import annotations

from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(ET)


def today_et() -> date:
    return now_et().date()


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def session_window(d: date, open_s: str, close_s: str) -> tuple[datetime, datetime]:
    """Return tz-aware (open, close) datetimes for date `d` in ET."""
    o = datetime.combine(d, parse_hhmm(open_s), ET)
    c = datetime.combine(d, parse_hhmm(close_s), ET)
    return o, c


def to_et(dt: datetime) -> datetime:
    """Coerce any datetime to ET (assume ET if naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


def fmt_et(dt: datetime) -> str:
    return to_et(dt).strftime("%H:%M ET")
