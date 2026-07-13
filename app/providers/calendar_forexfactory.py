"""Economic calendar via ForexFactory's free weekly JSON feed.

This is the same data the user already trusts. It's behind the CalendarProvider
interface so a paid, more reliable feed can be dropped in later.

Two properties of this feed drive the design around it (see app/calendar_store.py):
  * It is RATE-LIMITED. A handful of requests in quick succession earns a 429 that
    persists for minutes, so nothing may fetch it per-use or per-request.
  * It only ever serves the CURRENT week - there is no `ff_calendar_nextweek.json`
    (it 404s). Next week's events appear here only once FF rolls the feed over,
    some time after Friday's close.
Hence: one scheduled job fetches `events_week()` into the cache; everything else
reads the cache.
"""
from __future__ import annotations

from datetime import date, datetime

import requests

from ..timeutils import ET
from .base import CalendarEvent, CalendarProvider

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_HEADERS = {"User-Agent": "Mozilla/5.0 (TradeScale Phase1)"}


class ForexFactoryCalendarProvider(CalendarProvider):
    def __init__(self, country: str = "USD", timeout: int = 15):
        self.country = country
        self.timeout = timeout

    def _fetch(self) -> list[dict]:
        resp = requests.get(FF_URL, headers=_HEADERS, timeout=self.timeout)
        # 429 included: a rate-limited fetch MUST raise, never look like an empty week.
        resp.raise_for_status()
        return resp.json()

    def _parse(self, e: dict) -> dict | None:
        """One raw FF row -> a cache/gate event, or None if it has no usable date."""
        raw = e.get("date")
        ev_time = None
        if raw:
            try:
                ev_time = datetime.fromisoformat(raw).astimezone(ET)
            except (ValueError, TypeError):
                ev_time = None
        # An "All Day"/"Tentative" event still has a date, just no usable time.
        ev_date = ev_time.date() if ev_time else _raw_date(raw)
        if ev_date is None:
            return None
        return {
            "title": e.get("title", ""),
            "country": e.get("country", ""),
            "date": ev_date,
            "time": ev_time,
            "impact": e.get("impact", "") or "",
        }

    def events_week(self) -> list[dict]:
        """EVERY event in the current feed week (this provider's country only).

        Each event carries an explicit `date` as well as `time`, because all-day and
        tentative events have no time but still belong to a day. May raise on network
        error / rate limit - the caller decides what a failure means.
        """
        out = []
        for raw in self._fetch():
            if raw.get("country") != self.country:
                continue
            parsed = self._parse(raw)
            if parsed:
                out.append(parsed)
        return out

    def events_for(self, d: date) -> list[CalendarEvent]:
        """Return this provider's events for date `d`. May raise on network error.

        Kept for the CalendarProvider contract and the health check. The engine does
        NOT call this - it reads the cache, so that a feed outage can't be mistaken
        for an empty calendar.
        """
        return [
            CalendarEvent(title=e["title"], country=e["country"],
                          time=e["time"], impact=e["impact"])
            for e in self.events_week() if e["date"] == d
        ]


def _raw_date(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except (ValueError, TypeError):
        return None
