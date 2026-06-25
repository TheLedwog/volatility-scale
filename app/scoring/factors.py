"""Phase 1 rule-based chop factors.

Each factor outputs a *chop risk* in [0, 1] (higher = more likely to chop). The
engine blends them by the configured weights into a single `direction_quality`
0..100 score (higher = cleaner/more tradeable). These curves are transparent
placeholders meant to be tuned in Settings and later replaced by a trained model.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from ..market_calendar import structural_flags
from ..timeutils import session_window
from .categories import categorize  # noqa: F401  (kept for parity / future use)

FACTOR_LABELS = {
    "prior_day_efficiency": "Prior-day efficiency",
    "event_noise": "Scheduled event noise",
    "vix_regime": "VIX regime",
    "overnight_range": "Overnight efficiency",
    "structural_day": "Structural day",
}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _efficiency_ratio(series: pd.Series):
    s = series.dropna()
    if len(s) < 3:
        return None
    net = abs(float(s.iloc[-1]) - float(s.iloc[0]))
    path = float(s.diff().abs().sum())
    if path <= 0:
        return None
    return _clamp(net / path)


# --------------------------------------------------------------------------- #
# Context: gather the raw inputs the factors need. Every fetch is guarded so a
# missing data source degrades that factor to neutral instead of crashing.
# --------------------------------------------------------------------------- #
def build_context(cfg: dict, price, events: list[dict], d: date) -> dict:
    tickers = cfg["tickers"]
    sess = cfg["session"]
    ctx: dict = {
        "prior_er": None, "atr_pct": None, "vix": None, "vix3m": None,
        "overnight_er": None, "event_count": 0, "structural": {},
        "structural_count": 0,
    }

    # Prior-day efficiency (daily proxy) + ATR%
    try:
        df = price.daily_history(tickers["primary"], lookback_days=40)
        if df is not None and not df.empty:
            last = df.dropna().iloc[-1]
            rng = float(last["High"]) - float(last["Low"])
            if rng > 0:
                ctx["prior_er"] = _clamp(abs(float(last["Close"]) - float(last["Open"])) / rng)
            tr = pd.concat([
                df["High"] - df["Low"],
                (df["High"] - df["Close"].shift()).abs(),
                (df["Low"] - df["Close"].shift()).abs(),
            ], axis=1).max(axis=1)
            atr = float(tr.rolling(14).mean().dropna().iloc[-1])
            ctx["atr_pct"] = atr / float(df["Close"].dropna().iloc[-1]) * 100.0
    except Exception:  # noqa: BLE001
        pass

    # VIX level + 3-month term structure
    try:
        ctx["vix"] = price.last_close(tickers["vix"])
    except Exception:  # noqa: BLE001
        pass
    try:
        ctx["vix3m"] = price.last_close(tickers["vix3m"])
    except Exception:  # noqa: BLE001
        pass

    # Overnight efficiency from futures 5-min bars leading into the open
    try:
        fut = price.intraday(tickers["futures"], interval="5m", lookback_days=2)
        if fut is not None and not fut.empty:
            idx = fut.index
            if idx.tz is None:
                idx = idx.tz_localize("UTC")
            et_idx = idx.tz_convert("America/New_York")
            open_dt, _ = session_window(d, sess["open"], sess["close"])
            mask = (et_idx >= open_dt - timedelta(hours=15)) & (et_idx <= open_dt)
            overnight = fut.loc[mask]
            ctx["overnight_er"] = _efficiency_ratio(overnight["Close"]) if not overnight.empty else None
    except Exception:  # noqa: BLE001
        pass

    # Scheduled event noise (Medium+ USD events that day)
    try:
        ctx["event_count"] = sum(
            1 for e in events if str(e.get("impact", "")).lower() in ("high", "medium")
        )
    except Exception:  # noqa: BLE001
        pass

    # Structural day flags
    try:
        flags = structural_flags(d)
        ctx["structural"] = flags
        ctx["structural_count"] = sum(1 for v in flags.values() if v)
    except Exception:  # noqa: BLE001
        pass

    return ctx


# --------------------------------------------------------------------------- #
# Individual factor curves
# --------------------------------------------------------------------------- #
def _f_prior(ctx):
    er = ctx["prior_er"]
    if er is None:
        return 0.5, False, "no daily data"
    return _clamp(1 - er), True, f"prior-day efficiency {er:.2f}"


def _f_event_noise(ctx):
    n = ctx["event_count"]
    return _clamp(n / 4.0), True, f"{n} medium+ USD events scheduled"


def _f_vix(ctx):
    vix = ctx["vix"]
    if vix is None:
        return 0.5, False, "no VIX data"
    if vix < 13:
        base = 0.70
    elif vix < 17:
        base = 0.55
    elif vix < 22:
        base = 0.40
    elif vix < 30:
        base = 0.30
    else:
        base = 0.25
    note = f"VIX {vix:.1f}"
    vix3m = ctx["vix3m"]
    if vix3m:
        if vix > vix3m:  # backwardation = stress = more trend, less chop
            base -= 0.10
            note += " (backwardation)"
        else:
            note += " (contango)"
    return _clamp(base, 0.1, 0.9), True, note


def _f_overnight(ctx):
    oer = ctx["overnight_er"]
    if oer is None:
        return 0.5, False, "no overnight futures data"
    return _clamp(1 - oer), True, f"overnight efficiency {oer:.2f}"


def _f_structural(ctx):
    flags = ctx["structural"]
    n = ctx["structural_count"]
    active = [k.replace("_", " ") for k, v in flags.items() if v]
    detail = ", ".join(active) if active else "none"
    return _clamp(0.2 + 0.2 * n, 0.0, 0.95), True, detail


_FACTOR_FUNCS = {
    "prior_day_efficiency": _f_prior,
    "event_noise": _f_event_noise,
    "vix_regime": _f_vix,
    "overnight_range": _f_overnight,
    "structural_day": _f_structural,
}


def compute_factors(cfg: dict, ctx: dict) -> dict:
    weights = cfg["weights"]
    rows, total_w, weighted = [], 0.0, 0.0
    for name, func in _FACTOR_FUNCS.items():
        w = float(weights.get(name, 0.0))
        risk, available, detail = func(ctx)
        contribution = w * risk
        weighted += contribution
        total_w += w
        rows.append({
            "name": name,
            "label": FACTOR_LABELS[name],
            "risk": round(risk, 3),
            "weight": round(w, 3),
            "contribution": round(contribution, 3),
            "available": available,
            "detail": detail,
        })

    chop_risk = (weighted / total_w) if total_w > 0 else 0.5
    direction_quality = int(round(100 * (1 - chop_risk)))

    dead_day = False
    threshold = cfg["thresholds"].get("dead_day_range_pct", 0.0)
    if ctx["atr_pct"] is not None and ctx["atr_pct"] < threshold:
        dead_day = True

    return {
        "factors": rows,
        "chop_risk": round(chop_risk, 3),
        "direction_quality": direction_quality,
        "dead_day": dead_day,
        "atr_pct": ctx["atr_pct"],
        "breakdown_kind": "rules",
    }
