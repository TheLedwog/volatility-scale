"""Pluggable data providers (prices, calendar, news, LLM).

Swapping a free source for a paid one means adding a class here and pointing the
relevant `providers.*` config key at it — no changes to the scoring engine.
"""
from __future__ import annotations

from ..config import get_config
from .base import FallbackNewsProvider
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
    """Webz.io primary (token quota, immune to a shared host's IP reputation),
    GDELT as the backstop. GDELT stays in the chain even when Webz is configured:
    it needs no key, so a blown Webz quota or outage degrades to the old behaviour
    instead of blanking the news factor.
    """
    cfg = cfg or get_config()
    from ..config import webz_api_key

    name = cfg["news"].get("provider", "webz")
    chain: list = []
    if name == "webz":
        token = webz_api_key(cfg)
        if token:
            from .news_webz import WebzNewsProvider

            chain.append(WebzNewsProvider(cfg, token))
    chain.append(GDELTNewsProvider(cfg))
    return chain[0] if len(chain) == 1 else FallbackNewsProvider(chain)


def get_llm_provider(cfg: dict | None = None):
    cfg = cfg or get_config()
    # Only OpenAI in Phase 4; swap point for Claude/other.
    return OpenAILLMProvider()
