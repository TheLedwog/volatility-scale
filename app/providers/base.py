"""Abstract provider interfaces. Concrete classes implement these."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Optional, TypedDict

import pandas as pd


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
