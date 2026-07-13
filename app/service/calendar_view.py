"""Calendar reads shared by the gate, the JSON API and the dashboard.

Everything here is served from the cache (app/calendar_store.py) - no upstream calls -
so the frontend can poll /api/v1/calendar as hard as it likes.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from ..calendar_store import age_hours, events_between, events_for, last_fetch  # noqa: F401
from ..market_calendar import is_trading_day, next_trading_day
from ..scoring.gate import decide_gate
from ..timeutils import now_et, session_window

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def calendar_status(cfg: dict) -> dict:
    """Provenance + staleness of the cached calendar."""
    fetch = last_fetch()
    age = age_hours()
    max_age = float(cfg.get("calendar", {}).get("max_age_hours", 12))
    # A NEGATIVE age means the stored timestamp is in the future - clock skew, a DST
    # slip, a restored backup. We can't trust it, and "can't trust it" must fail toward
    # stale (protective), never toward fresh (which would let an empty cache clear the
    # gate). Same for never-fetched.
    return {
        "fetched_at": fetch.get("fetched_at"),
        "attempted_at": fetch.get("attempted_at"),
        "ok": bool(fetch.get("ok")) if fetch.get("ok") is not None else None,
        "error": fetch.get("error"),
        "age_hours": round(age, 2) if age is not None else None,
        "stale": age is None or age < 0 or age > max_age,
        "never_fetched": age is None,
    }


def gate_events(cfg: dict, d: date) -> tuple[list[dict], bool]:
    """The events the gate should score day `d` on, plus a `calendar_unavailable` flag.

    The flag is the whole point of the cache. An empty event list is AMBIGUOUS - it can
    mean "genuinely nothing scheduled" (a quiet day) or "we never got the calendar"
    (rate limit, outage, fresh deploy). Scoring the second as if it were the first is
    what let a rate-limited FOMC day grade CLEAN / "good to trade".

    An empty list is only trustworthy if we can show the calendar really is loaded for
    that day. "We fetched successfully at some point" is NOT enough:

        Monday. The last good fetch was Saturday - before ForexFactory rolled its
        one-week feed over on Sunday - so the cache holds LAST week and nothing for
        Monday. Monday's refreshes all fail. Empty events, but a non-null fetched_at.

    So we demand both: a fetch recent enough to be current (not stale), AND at least one
    event somewhere in this day's week (a USD week with zero events does not happen in
    practice - it means the week was never loaded). Fail either, and the day is treated
    as calendar-unavailable rather than clean.
    """
    events = events_for(d)
    if events:
        return events, False

    status = calendar_status(cfg)
    if status["never_fetched"] or status["stale"]:
        return [], True

    # Fresh fetch, but is THIS day's week actually in the cache? Guards the case above,
    # where a fetch lands before the feed has rolled over to the week containing `d`.
    monday = d - timedelta(days=d.weekday())
    week = events_between(monday, monday + timedelta(days=4))
    return [], not week


def week_ahead_mode(cfg: dict, now: datetime | None = None) -> bool:
    """True once this week's last session is done - i.e. show what's coming, not today.

    Friday after the close, and all weekend. Derived from the trading calendar rather
    than hardcoding "Friday", so a holiday-shortened week (last session Thursday)
    flips over on Thursday's close, which is the behaviour you'd want.
    """
    now = now or now_et()
    d = now.date()
    if is_trading_day(d):
        sess = cfg["session"]
        _open_dt, close_dt = session_window(d, sess["open"], sess["close"])
        if now < close_dt:
            return False  # today's session is still ahead of us / in progress
    # No session left today. Is the next one in a different ISO week?
    nxt = next_trading_day(d)
    return tuple(nxt.isocalendar()[:2]) != tuple(d.isocalendar()[:2])


def _week_ahead_range(d: date) -> tuple[date, date]:
    """Next session through the Friday of that session's week."""
    start = next_trading_day(d)
    end = start + timedelta(days=max(0, 4 - start.weekday()))  # Friday of start's week
    return start, end


def _day_block(cfg: dict, d: date, events: list[dict]) -> dict:
    """One day's events + the tier they imply, via the SAME gate the engine uses."""
    gate = decide_gate(events, cfg, d)
    return {
        "date": d.isoformat(),
        "weekday": _WEEKDAYS[d.weekday()],
        "is_trading_day": is_trading_day(d),
        "tier": gate["tier"],
        "reason": gate["reason"],
        "warn_note": gate["warn_note"],
        # Strip the raw datetime; `time_str` is the display value.
        "events": [{k: v for k, v in e.items() if k != "time"} for e in gate["events"]],
    }


def calendar_payload(cfg: dict, now: datetime | None = None) -> dict:
    """The /api/v1/calendar body.

    Available from 00:00 ET, long before the prediction runs, because the gate is
    calendar-only: the tier (VETO/WARN/CLEAN) needs no price data. The frontend can
    show "today is a VETO day" at midnight and fill the score in at the open.

    mode="today" on a normal day; mode="week_ahead" once the week's last session has
    closed, listing next week's sessions so Friday evening shows what's coming.
    """
    now = now or now_et()
    today = now.date()  # derive from `now`, so an injected clock moves the whole view
    ahead = week_ahead_mode(cfg, now)

    if ahead:
        start, end = _week_ahead_range(today)
        grouped = events_between(start, end)
        days = [
            _day_block(cfg, start + timedelta(days=i),
                       grouped.get((start + timedelta(days=i)).isoformat(), []))
            for i in range((end - start).days + 1)
        ]
    else:
        days = [_day_block(cfg, today, events_for(today))]

    status = calendar_status(cfg)
    total_events = sum(len(d["events"]) for d in days)

    return {
        "mode": "week_ahead" if ahead else "today",
        "generated_at": now.isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "calendar": status,
        # week_ahead with nothing in it = FF hasn't rolled its one-week feed over yet.
        # Distinct from "next week is quiet", so the frontend can say "not published yet".
        "awaiting_feed": bool(ahead and total_events == 0),
        "event_count": total_events,
        "days": days,
    }
