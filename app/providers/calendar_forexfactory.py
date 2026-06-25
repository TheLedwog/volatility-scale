"""Economic calendar via ForexFactory's free weekly JSON feed.

This is the same data the user already trusts. It's behind the CalendarProvider
interface so a paid, more reliable feed can be dropped in later.
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
        resp.raise_for_status()
        return resp.json()

    def events_for(self, d: date) -> list[CalendarEvent]:
        """Return this provider's events for date `d`. May raise on network error."""
        out: list[CalendarEvent] = []
        for e in self._fetch():
            if e.get("country") != self.country:
                continue
            ev_time = None
            raw = e.get("date")
            if raw:
                try:
                    ev_time = datetime.fromisoformat(raw).astimezone(ET)
                except (ValueError, TypeError):
                    ev_time = None
            # Use the event's ET date (fall back to the raw string's date).
            ev_date = ev_time.date() if ev_time else _raw_date(raw)
            if ev_date != d:
                continue
            out.append(
                CalendarEvent(
                    title=e.get("title", ""),
                    country=e.get("country", ""),
                    time=ev_time,
                    impact=e.get("impact", "") or "",
                )
            )
        return out


def _raw_date(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except (ValueError, TypeError):
        return None
