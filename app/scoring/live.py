"""Live intraday session tracker.

The morning verdict is a *frozen* pre-open forecast. This is the separate, live
companion: it reads how today's 9:30->now session is *actually* unfolding (the
same Kaufman Efficiency Ratio the post-close labeler uses) and says whether the
morning call is holding up or being challenged. It updates through the day; it
does NOT touch the frozen verdict.
"""
from __future__ import annotations

import time as _time

from ..config import get_config
from ..market_calendar import is_trading_day
from ..providers import get_price_provider
from ..timeutils import fmt_et, now_et, session_window, today_et

# Short cache so repeated page loads / polls don't refetch yfinance every time.
_bar_cache: dict = {"key": None, "ts": 0.0, "df": None}


def _get_bars(price, ticker: str, interval: str, ttl: float = 60.0):
    now = _time.time()
    key = (ticker, interval)
    if (_bar_cache["key"] == key and _bar_cache["df"] is not None
            and (now - _bar_cache["ts"]) < ttl):
        return _bar_cache["df"]
    df = price.intraday(ticker, interval=interval, lookback_days=2)
    _bar_cache.update(key=key, ts=now, df=df)
    return df


def _call_check(live_label: str, pred: dict | None, cfg: dict) -> dict | None:
    """One-line read of whether the morning call is holding up vs. live action."""
    if not pred or pred.get("tier") == "CLOSED":
        if not pred:
            return {"tone": "neutral", "text": "No morning call recorded for today yet."}
        return None
    th = cfg["thresholds"]
    tier, dq = pred.get("tier"), pred.get("direction_quality")
    if tier == "VETO" or (dq is not None and dq < th["caution"]):
        expected = "avoid"
    elif dq is not None and dq >= th["good"]:
        expected = "trade"
    else:
        expected = "mixed"

    trending, chopping = live_label == "TRENDING", live_label == "CHOPPY"
    if expected == "avoid":
        if trending:
            return {"tone": "challenged",
                    "text": "Morning call was AVOID, but it's trending so far - the call is being challenged."}
        return {"tone": "holding",
                "text": "Holding up - choppy/mixed, as the morning AVOID call expected."}
    if expected == "trade":
        if chopping:
            return {"tone": "challenged",
                    "text": "Morning call was TRADE, but it's chopping so far - stay alert."}
        return {"tone": "holding",
                "text": "Holding up - directional, as the morning call expected."}
    return {"tone": "neutral",
            "text": "Morning call was mixed; the session is " + live_label.lower() + " so far."}


def live_session(cfg: dict | None = None, pred: dict | None = None) -> dict:
    cfg = cfg or get_config()
    d = today_et()
    if not is_trading_day(d):
        return {"state": "closed_day"}

    sess = cfg["session"]
    th = cfg["thresholds"]
    now = now_et()
    open_dt, close_dt = session_window(d, sess["open"], sess["close"])
    if now < open_dt:
        return {"state": "pre_open", "open_str": fmt_et(open_dt)}

    try:
        df = _get_bars(get_price_provider(), cfg["tickers"]["primary"], "5m")
    except Exception as exc:  # noqa: BLE001
        return {"state": "error", "error": str(exc), "as_of": fmt_et(now)}
    if df is None or df.empty:
        return {"state": "waiting", "as_of": fmt_et(now)}

    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    et_idx = idx.tz_convert("America/New_York")
    end = min(now, close_dt)
    s = df.loc[(et_idx >= open_dt) & (et_idx <= end)]
    if len(s) < 2:
        return {"state": "waiting", "as_of": fmt_et(now)}

    closes = s["Close"].astype(float)
    session_open = float(s["Open"].astype(float).iloc[0])
    last = float(closes.iloc[-1])
    net = abs(last - session_open)
    path = float(closes.diff().abs().sum()) + abs(float(closes.iloc[0]) - session_open)
    er = (net / path) if path > 0 else 0.0
    pct_move = ((last - session_open) / session_open * 100.0) if session_open else 0.0

    if er >= th["label_directional_er"]:
        label = "TRENDING"
    elif er <= th["label_choppy_er"]:
        label = "CHOPPY"
    else:
        label = "MIXED"

    after_close = now >= close_dt
    return {
        "state": "after_close" if after_close else "live",
        "er": round(er, 3),
        "dq": int(round(er * 100)),
        "label": label,
        "pct_move": round(pct_move, 2),
        "bars": int(len(s)),
        "as_of": fmt_et(end),
        "call_check": _call_check(label, pred, cfg),
    }
