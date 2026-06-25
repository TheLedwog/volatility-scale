"""Free price/VIX/futures data via yfinance."""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .base import PriceProvider


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance sometimes returns MultiIndex columns even for a single ticker."""
    if df is None or df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    return df


class YFinancePriceProvider(PriceProvider):
    def daily_history(self, ticker: str, lookback_days: int = 60) -> pd.DataFrame:
        import yfinance as yf

        df = yf.download(
            ticker, period=f"{lookback_days}d", interval="1d",
            progress=False, auto_adjust=False,
        )
        return _flatten(df)

    def intraday(self, ticker: str, interval: str = "5m",
                 lookback_days: int = 5) -> pd.DataFrame:
        import yfinance as yf

        df = yf.download(
            ticker, period=f"{lookback_days}d", interval=interval,
            progress=False, auto_adjust=False,
        )
        return _flatten(df)

    def last_close(self, ticker: str) -> Optional[float]:
        df = self.daily_history(ticker, lookback_days=7)
        if df is None or df.empty or "Close" not in df:
            return None
        return float(df["Close"].dropna().iloc[-1])
