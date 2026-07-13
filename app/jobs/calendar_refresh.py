"""The ONLY thing that fetches the economic calendar upstream.

Everything else (the gate, /api/v1/calendar, the dashboard) reads the cache. Keeping
the network access in one scheduled job is what keeps us under ForexFactory's rate
limit and stops a 429 from ever being mistaken for "nothing is scheduled today".

Runs EVERY day, not just weekdays: FF rolls its one-week feed over to the new week
some time after Friday's close, and the weekend refreshes are what pull next week's
events into the cache for the Friday/weekend week-ahead view.
"""
from __future__ import annotations

from datetime import timedelta

from ..calendar_store import (
    age_hours,
    events_between,
    has_events_for,
    record_fetch,
    upsert_events,
)
from ..providers import get_calendar_provider
from ..timeutils import now_et, today_et


def refresh_calendar() -> dict:
    """Pull the current FF week into the cache. Never raises.

    A failure is recorded and the existing cache is left intact - a stale calendar is
    always better than no calendar, because "no calendar" reads as "no events" and
    silently clears the gate.
    """
    try:
        provider = get_calendar_provider()
        events = provider.events_week()
    except Exception as exc:  # noqa: BLE001 - network/rate-limit/parse
        record_fetch(ok=False, error=str(exc))
        return {"ok": False, "error": str(exc), "stored": 0}

    stored = upsert_events(events, now_et())
    record_fetch(ok=True, events=len(events))
    return {"ok": True, "error": None, "stored": stored, "fetched": len(events)}


def ensure_calendar(cfg: dict, d=None) -> dict:
    """Refresh only if the cache looks unusable for day `d`; otherwise do nothing.

    Called before a prediction so a fresh deploy (empty DB) still gets a calendar,
    without putting an upstream call on every prediction.
    """
    d = d or today_et()
    max_age = float(cfg.get("calendar", {}).get("max_age_hours", 12))
    age = age_hours()

    if age is None:
        return refresh_calendar()                    # never fetched
    if age > max_age:
        return refresh_calendar()                    # stale

    # Cache is fresh, but does it actually hold the week `d` lives in? It might not:
    # a fetch that lands before ForexFactory rolls its one-week feed over returns the
    # PREVIOUS week, leaving `d`'s week missing while the cache still looks fresh. A
    # USD week with zero events doesn't happen in practice, so an empty week means the
    # week was never loaded - worth one fetch to try to fix it.
    monday = d - timedelta(days=d.weekday())
    if not events_between(monday, monday + timedelta(days=4)):
        return refresh_calendar()

    if not has_events_for(d):
        # The week is loaded, this day is simply empty (a holiday, a quiet Monday).
        # That's real information, not a gap. Don't burn a fetch on it.
        return {"ok": True, "error": None, "stored": 0, "skipped": "week loaded, day empty"}
    return {"ok": True, "error": None, "stored": 0, "skipped": "cache fresh"}
