"""Pluggable data providers (prices, calendar, news, LLM).

Swapping a free source for a paid one means adding a class here and pointing the
relevant `providers.*` config key at it — no changes to the scoring engine.
"""
from __future__ import annotations

from ..config import get_config
from .calendar_forexfactory import ForexFactoryCalendarProvider
from .prices_yfinance import YFinancePriceProvider


def get_price_provider():
    return YFinancePriceProvider()


def get_calendar_provider():
    cfg = get_config()
    name = cfg["providers"].get("calendar", "forexfactory")
    # Only one implementation in Phase 1; this is the swap point.
    if name == "forexfactory":
        return ForexFactoryCalendarProvider()
    return ForexFactoryCalendarProvider()
