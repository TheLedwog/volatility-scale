"""Post-close labeling: measure how directional the NY session actually was.

Uses 5-min bars for the 9:30-16:00 ET window and computes the Kaufman Efficiency
Ratio (net move / total path). This is the ground-truth label the tool will be
calibrated and (Phase 3) trained against.
"""
from __future__ import annotations

from datetime import date

from datetime import datetime

from ..config import get_config
from ..db import get_conn
from ..market_calendar import is_trading_day, prev_trading_day
from ..providers import get_price_provider
from ..timeutils import ET, now_et, parse_hhmm, session_window, today_et


def _window_er(s) -> float | None:
    """Kaufman efficiency ratio over a slice of 5-min bars, or None if too short.

    net move / total path, using the first bar's Open as the window's origin so a
    gap into the window still counts toward the path (same convention the live
    tracker and the full-session labeler have always used).
    """
    if s is None or len(s) < 2:
        return None
    closes = s["Close"].astype(float)
    w_open = float(s["Open"].astype(float).iloc[0])
    net = abs(float(closes.iloc[-1]) - w_open)
    path = float(closes.diff().abs().sum()) + abs(float(closes.iloc[0]) - w_open)
    return (net / path) if path > 0 else 0.0


def blended_session_er(s, d: date, cfg: dict) -> tuple[float, float | None, float | None]:
    """Grade a session slice `s` (5-min bars) as a morning/afternoon ER blend.

    Returns (blended_er, morning_er, afternoon_er). Splitting the day and blending
    stops a clean morning that round-trips after lunch from grading as all-day chop
    (see the `grading` config section). Degrades to whichever half exists (early
    close / thin data), and finally to a single full-session ER, so it never fails.
    """
    grading = cfg.get("grading", {})
    w = float(grading.get("morning_weight", 0.7))
    split_dt = datetime.combine(d, parse_hhmm(grading.get("split_time", "12:00")), ET)

    idx = s.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    et = idx.tz_convert("America/New_York")
    morning_er = _window_er(s.loc[et < split_dt])
    afternoon_er = _window_er(s.loc[et >= split_dt])

    if morning_er is not None and afternoon_er is not None:
        blended = w * morning_er + (1.0 - w) * afternoon_er
    elif morning_er is not None:
        blended = morning_er
    elif afternoon_er is not None:
        blended = afternoon_er
    else:
        blended = _window_er(s) or 0.0
    return blended, morning_er, afternoon_er


def _default_label_date() -> date:
    """Most recent completed session."""
    today = today_et()
    cfg = get_config()
    close_t = parse_hhmm(cfg["session"]["close"])
    if is_trading_day(today) and now_et().time() >= close_t:
        return today
    return prev_trading_day(today)


def _session_slice(df, d: date, sess: dict):
    """The 9:30-16:00 ET 5-min bars for day `d` out of a wider intraday frame."""
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    et_idx = idx.tz_convert("America/New_York")
    open_dt, close_dt = session_window(d, sess["open"], sess["close"])
    return df.loc[(et_idx >= open_dt) & (et_idx <= close_dt)]


def _grade_slice(s, d: date, cfg: dict, th: dict) -> dict:
    """Turn a session slice into the stored outcome dict (does not persist).

    Grades on the morning/afternoon ER blend (see grading config); range stays a
    full-session "how big was the day" measure, not a directionality claim.
    """
    er, morning_er, afternoon_er = blended_session_er(s, d, cfg)
    session_open = float(s["Open"].astype(float).iloc[0])
    rng = float(s["High"].astype(float).max() - s["Low"].astype(float).min())
    range_pct = (rng / session_open * 100.0) if session_open else 0.0

    if er >= th["label_directional_er"]:
        label = "DIRECTIONAL"
    elif er <= th["label_choppy_er"]:
        label = "CHOPPY"
    else:
        label = "MIXED"

    return {
        "date": d.isoformat(), "realized_er": round(er, 3),
        "morning_er": round(morning_er, 3) if morning_er is not None else None,
        "afternoon_er": round(afternoon_er, 3) if afternoon_er is not None else None,
        "realized_range": round(rng, 2), "range_pct": round(range_pct, 3),
        "realized_label": label, "bars": int(len(s)),
    }


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

    s = _session_slice(df, d, sess)
    if len(s) < 5:
        return {"date": d.isoformat(), "error": "session not available yet (need 5-min bars)"}

    result = _grade_slice(s, d, cfg, th)
    _store(result)
    return result


def run_regrade(days: int = 60) -> dict:
    """Re-label every stored session in the last `days` with the CURRENT thresholds.

    Use after changing the label cutoffs (or the grading blend) so the existing
    track record / calibration reflect the new definition instead of the old one.
    yfinance only serves ~60 days of 5-min bars, so older stored outcomes cannot
    be recomputed and are left untouched. One fetch, then grade each trading day.
    """
    cfg = get_config()
    sess = cfg["session"]
    th = cfg["thresholds"]
    price = get_price_provider()
    lookback = max(7, min(int(days), 60))

    try:
        df = price.intraday(cfg["tickers"]["primary"], interval="5m", lookback_days=lookback)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"intraday fetch failed: {exc}", "regraded": []}
    if df is None or df.empty:
        return {"error": "no intraday data", "regraded": []}

    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    trading_days = sorted({ts.date() for ts in idx.tz_convert("America/New_York")})

    # Only re-label days we ALREADY have an outcome for - re-grade means recompute
    # the existing track record, not invent labels for days that were never graded.
    conn = get_conn()
    try:
        existing = {row[0] for row in conn.execute("SELECT date FROM outcomes")}
    finally:
        conn.close()

    regraded, skipped = [], []
    for d in trading_days:
        if not is_trading_day(d) or d.isoformat() not in existing:
            continue
        s = _session_slice(df, d, sess)
        if len(s) < 5:
            skipped.append(d.isoformat())
            continue
        r = _grade_slice(s, d, cfg, th)
        _store(r)
        regraded.append({"date": r["date"], "realized_er": r["realized_er"],
                         "realized_label": r["realized_label"]})
    return {"regraded": regraded, "skipped": skipped, "days": lookback}


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
