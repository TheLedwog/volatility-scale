"""Pluggable data providers (prices, calendar, news, LLM).

Swapping a free source for a paid one means adding a class here and pointing the
relevant `providers.*` config key at it — no changes to the scoring engine.
"""
from __future__ import annotations

from ..config import get_config
from .calendar_forexfactory import ForexFactoryCalendarProvider
from .llm_openai import OpenAILLMProvider
from .news_gdelt import GDELTNewsProvider
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


def get_news_provider(cfg: dict | None = None):
    cfg = cfg or get_config()
    # Only GDELT in Phase 4; this is the swap point for a paid feed.
    return GDELTNewsProvider(cfg)


def get_llm_provider(cfg: dict | None = None):
    cfg = cfg or get_config()
    # Only OpenAI in Phase 4; swap point for Claude/other.
    return OpenAILLMProvider()
