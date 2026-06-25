"""Canonical feature builder + session-ER labels.

The SAME `feature_row` function is used both to build the training table and to
score live, so there is no train/serve skew. Every feature is knowable by the
prior session's close (or is a calendar attribute of the day), so it's fully
available pre-open and contains no lookahead.
"""
from __future__ import annotations

from datetime import date, time

import numpy as np
import pandas as pd

from ..market_calendar import structural_flags
from ..timeutils import parse_hhmm

FEATURE_COLUMNS = [
    "dow", "month",
    "prior_er", "prior_ret", "prior_range_pct", "atr14_pct",
    "ret_5d", "vol_5d",
    "vix", "vix_chg", "vix_ts",
    "opex", "quad_witching", "day_before_holiday", "day_after_holiday",
    "month_end", "quarter_end", "early_close",
]

FEATURE_LABELS = {
    "dow": "Day of week", "month": "Month",
    "prior_er": "Prior-day efficiency", "prior_ret": "Prior-day return",
    "prior_range_pct": "Prior-day range %", "atr14_pct": "ATR(14) %",
    "ret_5d": "5-day return", "vol_5d": "5-day volatility",
    "vix": "VIX level", "vix_chg": "VIX change", "vix_ts": "VIX term structure",
    "opex": "OPEX", "quad_witching": "Quad witching",
    "day_before_holiday": "Day before holiday", "day_after_holiday": "Day after holiday",
    "month_end": "Month end", "quarter_end": "Quarter end", "early_close": "Early close",
    "news_chop_risk": "News chop-risk", "news_impact": "News impact",
    "news_relevance": "News relevance", "news_dir": "News direction",
}

# Optional news features, only present once the historical news backfill has run.
NEWS_FEATURE_COLUMNS = ["news_chop_risk", "news_impact", "news_relevance", "news_dir"]


def news_feature_dict(news: dict | None) -> dict:
    """Map a news assessment to numeric model features (NaN if not GPT-scored)."""
    if news and news.get("scored"):
        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return np.nan

        dir_map = {"risk_off": -1.0, "risk_on": 1.0}
        return {
            "news_chop_risk": _num(news.get("chop_risk")),
            "news_impact": _num(news.get("expected_impact")),
            "news_relevance": _num(news.get("relevance")),
            "news_dir": dir_map.get(news.get("direction"), 0.0),
        }
    return {c: np.nan for c in NEWS_FEATURE_COLUMNS}


def to_date_index(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy indexed by python `date` (drops tz), sorted ascending."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out.index = [ts.date() if hasattr(ts, "date") else ts for ts in out.index]
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def _last_before(df: pd.DataFrame, d: date):
    if df is None or df.empty:
        return None
    prior = df[df.index < d]
    return prior if not prior.empty else None


def feature_row(daily: pd.DataFrame, vix: pd.DataFrame, vix3m: pd.DataFrame,
                d: date) -> dict | None:
    """Build the feature dict for session date `d`. All inputs date-indexed.

    Returns None if there isn't enough prior history.
    """
    prior = _last_before(daily, d)
    if prior is None or len(prior) < 20:
        return None

    last = prior.iloc[-1]
    o, h, l, c = float(last["Open"]), float(last["High"]), float(last["Low"]), float(last["Close"])
    rng = h - l
    closes = prior["Close"].astype(float)

    # ATR(14)% as of the prior close
    tr = pd.concat([
        prior["High"] - prior["Low"],
        (prior["High"] - prior["Close"].shift()).abs(),
        (prior["Low"] - prior["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    atr_pct = (atr / c * 100.0) if c else np.nan

    row = {
        "dow": d.weekday(),
        "month": d.month,
        "prior_er": (abs(c - o) / rng) if rng > 0 else np.nan,
        "prior_ret": (c / o - 1.0) if o else np.nan,
        "prior_range_pct": (rng / o * 100.0) if o else np.nan,
        "atr14_pct": atr_pct,
        "ret_5d": (closes.pct_change(5).iloc[-1] * 100.0) if len(closes) > 5 else np.nan,
        "vol_5d": (closes.pct_change().rolling(5).std().iloc[-1] * 100.0) if len(closes) > 5 else np.nan,
    }

    vp = _last_before(vix, d)
    if vp is not None:
        vc = vp["Close"].astype(float)
        row["vix"] = float(vc.iloc[-1])
        row["vix_chg"] = float(vc.diff().iloc[-1]) if len(vc) > 1 else np.nan
    else:
        row["vix"] = np.nan
        row["vix_chg"] = np.nan

    v3 = _last_before(vix3m, d)
    if v3 is not None and not np.isnan(row["vix"]):
        row["vix_ts"] = row["vix"] - float(v3["Close"].astype(float).iloc[-1])
    else:
        row["vix_ts"] = np.nan

    flags = structural_flags(d)
    for k in ("opex", "quad_witching", "day_before_holiday", "day_after_holiday",
              "month_end", "quarter_end", "early_close"):
        row[k] = int(bool(flags.get(k, False)))

    return row


def session_labels(hourly: pd.DataFrame, open_s: str = "09:30",
                   close_s: str = "16:00", min_bars: int = 4) -> dict[date, tuple]:
    """Compute (efficiency_ratio, range_pct, n_bars) per session date from hourly bars."""
    if hourly is None or hourly.empty:
        return {}
    idx = hourly.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    et = idx.tz_convert("America/New_York")

    h = hourly.copy()
    h["_d"] = [t.date() for t in et]
    h["_tod"] = [t.time() for t in et]
    o_t, c_t = parse_hhmm(open_s), parse_hhmm(close_s)
    h = h[[(t >= o_t) and (t <= c_t) for t in h["_tod"]]]

    out: dict[date, tuple] = {}
    for d, g in h.groupby("_d"):
        g = g.sort_values("_tod")
        if len(g) < min_bars:
            continue
        closes = g["Close"].astype(float)
        so = float(g["Open"].astype(float).iloc[0])
        sc = float(closes.iloc[-1])
        net = abs(sc - so)
        path = float(closes.diff().abs().sum()) + abs(float(closes.iloc[0]) - so)
        er = (net / path) if path > 0 else 0.0
        rng = float(g["High"].astype(float).max() - g["Low"].astype(float).min())
        out[d] = (er, (rng / so * 100.0) if so else 0.0, int(len(g)))
    return out


def build_training_frame(daily, vix, vix3m, hourly, min_bars: int = 4) -> pd.DataFrame:
    """Assemble the full features + label table (one row per labeled session)."""
    daily_d = to_date_index(daily)
    vix_d = to_date_index(vix)
    vix3m_d = to_date_index(vix3m)
    labels = session_labels(hourly, min_bars=min_bars)

    rows = []
    for d in sorted(labels.keys()):
        feats = feature_row(daily_d, vix_d, vix3m_d, d)
        if feats is None:
            continue
        er, range_pct, bars = labels[d]
        feats = dict(feats)
        feats.update({"date": d.isoformat(), "session_er": er,
                      "range_pct": range_pct, "bars": bars})
        rows.append(feats)

    return pd.DataFrame(rows)
