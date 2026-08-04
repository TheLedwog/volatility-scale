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
    """Build the news feed chain from `news.provider_chain`, in order.

    Default is GDELT then Webz, and the order is the whole point: GDELT classifies
    by GKG theme so it finds catalysts a title-keyword query would miss, while Webz
    is token-authenticated so it still answers when GDELT rate-limits the shared
    host IP. Each covers the other's real weakness.

    A feed with no usable credentials is skipped rather than failing the chain, so
    an unset Webz token just means GDELT-only.
    """
    cfg = cfg or get_config()
    from ..config import webz_api_key

    names = cfg["news"].get("provider_chain") or [cfg["news"].get("provider", "gdelt")]
    chain: list = []
    for name in names:
        if name == "gdelt":
            chain.append(GDELTNewsProvider(cfg))
        elif name == "webz":
            token = webz_api_key(cfg)
            if token:
                from .news_webz import WebzNewsProvider

                chain.append(WebzNewsProvider(cfg, token))
    if not chain:  # misconfigured chain shouldn't mean no news at all
        chain.append(GDELTNewsProvider(cfg))
    return chain[0] if len(chain) == 1 else FallbackNewsProvider(chain)


def get_llm_provider(cfg: dict | None = None):
    cfg = cfg or get_config()
    # Only OpenAI in Phase 4; swap point for Claude/other.
    return OpenAILLMProvider()
