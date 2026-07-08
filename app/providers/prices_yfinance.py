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


_YF_CONFIGURED = False


def _configure_yfinance() -> None:
    """Force yfinance's 'csrf' cookie strategy so it never touches fc.yahoo.com.

    yfinance's default 'basic' strategy fetches its auth cookie from
    ``fc.yahoo.com``, whose TLS certificate does not list that hostname as a
    SAN. From a datacenter IP with no cached cookie (e.g. Railway) the handshake
    raises ``CertificateVerifyError``; because that's an exception rather than a
    ``None`` return, yfinance's built-in basic->csrf fallback never fires and
    every fetch fails hard. Forcing 'csrf' up front uses
    ``guce.yahoo.com/consent`` instead, sidesteps the broken host entirely, and
    keeps SSL verification on. Best-effort and idempotent: this reaches into a
    private API, so we guard it and degrade to yfinance's default if it changes.
    """
    global _YF_CONFIGURED
    if _YF_CONFIGURED:
        return
    try:
        from yfinance.data import YfData

        YfData()._set_cookie_strategy("csrf")
    except Exception:
        pass
    _YF_CONFIGURED = True


class YFinancePriceProvider(PriceProvider):
    def daily_history(self, ticker: str, lookback_days: int = 60) -> pd.DataFrame:
        import yfinance as yf

        _configure_yfinance()
        df = yf.download(
            ticker, period=f"{lookback_days}d", interval="1d",
            progress=False, auto_adjust=False,
        )
        return _flatten(df)

    def intraday(self, ticker: str, interval: str = "5m",
                 lookback_days: int = 5) -> pd.DataFrame:
        import yfinance as yf

        _configure_yfinance()
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
