"""Post-close labeling: measure how directional the NY session actually was.

Uses 5-min bars for the 9:30-16:00 ET window and computes the Kaufman Efficiency
Ratio (net move / total path). This is the ground-truth label the tool will be
calibrated and (Phase 3) trained against.
"""
from __future__ import annotations

from datetime import date

from ..config import get_config
from ..db import get_conn
from ..market_calendar import is_trading_day, prev_trading_day
from ..providers import get_price_provider
from ..timeutils import now_et, parse_hhmm, session_window, today_et


def _default_label_date() -> date:
    """Most recent completed session."""
    today = today_et()
    cfg = get_config()
    close_t = parse_hhmm(cfg["session"]["close"])
    if is_trading_day(today) and now_et().time() >= close_t:
        return today
    return prev_trading_day(today)


def run_labeling(d: date | None = None) -> dict:
    cfg = get_config()
    d = d or _default_label_date()
    sess = cfg["session"]
    th = cfg["thresholds"]
    price = get_price_provider()

    try:
        df = price.intraday(cfg["tickers"]["primary"], interval="5m", lookback_days=7)
    except Exception as exc:  # noqa: BLE001
        return {"date": d.isoformat(), "error": f"intraday fetch failed: {exc}"}

    if df is None or df.empty:
        return {"date": d.isoformat(), "error": "no intraday data"}

    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    et_idx = idx.tz_convert("America/New_York")
    open_dt, close_dt = session_window(d, sess["open"], sess["close"])
    mask = (et_idx >= open_dt) & (et_idx <= close_dt)
    s = df.loc[mask]

    if len(s) < 5:
        return {"date": d.isoformat(), "error": "session not available yet (need 5-min bars)"}

    closes = s["Close"].astype(float)
    session_open = float(s["Open"].astype(float).iloc[0])
    session_close = float(closes.iloc[-1])
    net = abs(session_close - session_open)
    path = float(closes.diff().abs().sum()) + abs(float(closes.iloc[0]) - session_open)
    er = (net / path) if path > 0 else 0.0
    rng = float(s["High"].astype(float).max() - s["Low"].astype(float).min())
    range_pct = (rng / session_open * 100.0) if session_open else 0.0

    if er >= th["label_directional_er"]:
        label = "DIRECTIONAL"
    elif er <= th["label_choppy_er"]:
        label = "CHOPPY"
    else:
        label = "MIXED"

    result = {
        "date": d.isoformat(), "realized_er": round(er, 3),
        "realized_range": round(rng, 2), "range_pct": round(range_pct, 3),
        "realized_label": label, "bars": int(len(s)),
    }
    _store(result)
    return result


def _store(r: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO outcomes
                (date, realized_er, realized_range, range_pct, realized_label,
                 bars, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                realized_er=excluded.realized_er,
                realized_range=excluded.realized_range,
                range_pct=excluded.range_pct,
                realized_label=excluded.realized_label,
                bars=excluded.bars, computed_at=excluded.computed_at
            """,
            (r["date"], r["realized_er"], r["realized_range"], r["range_pct"],
             r["realized_label"], r["bars"], now_et().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()
