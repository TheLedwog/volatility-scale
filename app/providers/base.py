"""Abstract provider interfaces. Concrete classes implement these."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Optional, TypedDict

import pandas as pd

# --- Market-relevance filter ------------------------------------------------ #
# Shared by the news providers and the display layer so obviously off-topic
# headlines (celebrity, gadgets, general politics) never reach the GPT read or the
# UI. Word-boundary regex, so "rate" won't match "celebrate". Intentionally errs
# toward finance: the provider's topic query is the primary filter, this is the net.
_FINANCE_PATTERNS = [
    r"stocks?", r"shares?", r"equit(?:y|ies)", r"markets?", r"wall\s*st",
    r"nasdaq", r"s&p", r"dow\s+jones", r"\bftse\b", r"nikkei", r"indices", r"stock\s+index",
    r"fed(?:eral\s+reserve)?", r"\bfomc\b", r"central\s+bank", r"\becb\b", r"\bboj\b", r"powell",
    r"interest\s+rates?", r"rate\s+(?:hike|cut|decision|rise|move)", r"yields?", r"bonds?",
    r"treasur(?:y|ies)", r"inflation", r"\bcpi\b", r"\bppi\b", r"\bpce\b", r"\bgdp\b",
    r"recession", r"payrolls?", r"jobless", r"unemployment", r"earnings", r"revenue", r"profit",
    r"dollar", r"currenc(?:y|ies)", r"\bforex\b", r"\beuro\b", r"\byen\b", r"sterling",
    r"bitcoin", r"crypto", r"\boil\b", r"crude", r"brent", r"\bopec\b", r"\bgold\b",
    r"commodit(?:y|ies)", r"tariffs?", r"trade\s+war", r"sanctions?", r"econom(?:y|ic|ies)",
    r"investors?", r"investing", r"bull(?:ish)?", r"bear(?:ish)?", r"sell[-\s]?off",
    r"rall(?:y|ies)", r"futures", r"hedge\s+fund", r"\bipo\b", r"buyback", r"dividend",
    r"valuation", r"consumer\s+(?:confidence|sentiment)", r"retail\s+sales", r"manufacturing",
    r"\bpmi\b", r"housing", r"mortgage", r"debt\s+ceiling", r"stimulus", r"monetary", r"fiscal",
]
_FINANCE_RE = re.compile("(?:%s)" % "|".join(_FINANCE_PATTERNS), re.IGNORECASE)


def market_relevant(title: str, extra_terms: Optional[list[str]] = None) -> bool:
    t = (title or "").strip()
    if not t:
        return False
    if _FINANCE_RE.search(t):
        return True
    if extra_terms:
        low = t.lower()
        return any(term.lower() in low for term in extra_terms if term)
    return False


def filter_market_headlines(titles: list[str], extra_terms: Optional[list[str]] = None) -> list[str]:
    return [t for t in titles if market_relevant(t, extra_terms)]


class CalendarEvent(TypedDict):
    title: str
    country: str
    time: Optional[datetime]   # tz-aware ET, or None if "All Day"/"Tentative"
    impact: str                # "High" | "Medium" | "Low" | "Holiday" | ""


class PriceProvider(ABC):
    @abstractmethod
    def daily_history(self, ticker: str, lookback_days: int = 60) -> pd.DataFrame: ...

    @abstractmethod
    def intraday(self, ticker: str, interval: str = "5m",
                 lookback_days: int = 5) -> pd.DataFrame: ...

    @abstractmethod
    def last_close(self, ticker: str) -> Optional[float]: ...


class CalendarProvider(ABC):
    @abstractmethod
    def events_for(self, d: date) -> list[CalendarEvent]: ...


class NewsProvider(ABC):
    @abstractmethod
    def headlines(self, d: date) -> list[str]: ...


class LLMProvider(ABC):
    @abstractmethod
    def score_news(self, headlines: list[str], cfg: dict) -> dict:
        """Return {scored, relevance, expected_impact, direction, chop_risk, rationale}."""
        ...
