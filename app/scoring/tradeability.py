"""Tradeability engine: score the day by the MOVE it is likely to offer.

Why this exists
---------------
The rule-based factor blend (`factors.py`) scores "how likely is the day to chop".
Measured over 723 hourly sessions and again over 2,491 daily ones with a
chronological train/test split, five of its six factors sit at the noise floor and
the blended score carries no out-of-sample signal: spearman(score, realized move)
= -0.002 on 218 unseen sessions. Worse, a weighted average of uninformative inputs
concentrates on its own mean, so the score collapsed to sd ~9.6 around 53 and could
not reach its own decision thresholds - 10 consecutive sessions graded "Mixed".

Two findings drive the redesign:

1. **Chop-vs-trend is close to unpredictable pre-open.** Out of ~40 candidate
   features the only survivor is multi-day efficiency mean-reversion (er_ma20:
   train -0.116, test -0.172). Yesterday's efficiency alone does NOT survive
   (train -0.108, test +0.008) - which is the 0.30-weight factor in the old blend,
   in both its original and its flipped form.
2. **Day SIZE is strongly predictable.** VIX vs realised range replicates at
   +0.535 train / +0.515 test; prior range, ATR, distance above the 20-day mean and
   the short-vs-long vol ratio all land in the same band. This is volatility
   clustering and it is about as robust as market regularities get.

So the target is the **net move**, |close - open|, which is what a directional
trader can actually capture. It is the right target for the product because it
punishes BOTH failure modes at once: a day that chops closes where it opened (net
~ 0) and a dead day never travels (net ~ 0). Chop is not abandoned here - it is
measured through the thing it destroys.

The prediction is deliberately factored into two legs so it stays explainable:

    expected_net = expected_range x expected_efficiency

The range leg is the strong one; the efficiency leg is weak but real and is the
only part that carries the chop signal. Their product beats neither leg alone and
matches an unfactored ridge on the same features (+0.324 vs +0.329 on the test
half), so the interpretable form costs nothing.

Scale
-----
The raw prediction is a percentage that means nothing to a reader, so the 1-100
score is a **percentile**. It is ranked against the distribution of REALISED net
moves, not against other predictions, and that distinction is load-bearing:

    score 60  ==  "we expect today to land around the 60th-percentile day"

Ranking predictions against each other was tried first and is wrong. Predictions
are far less dispersed than outcomes (a model this strength forecasts a narrow band
around the mean), so a mildly-below-average forecast sits at the very bottom of the
prediction distribution while the day it describes is merely slightly quiet. In
August 2026 that produced scores of 1 for a week of sessions whose realised moves
were at the 38th percentile of ten years - a calibration artefact reading as a dead
market. Ranking against outcomes fixes it at no measurable cost to discrimination
(walk-forward rho +0.366 vs +0.374; top/bottom band separation actually improves,
3.07x vs 2.62x).

The consequence to accept: the score's own range is compressed (roughly 25-100 in
back-test) because a weak predictor is not entitled to claim extreme percentiles.
The band cuts below are set on that compressed distribution, not on a naive 0-100.

Everything here is knowable at 09:30 ET. Every feature is built from COMPLETED
prior sessions (`.shift(1)`), so there is no lookahead and no train/serve skew -
the same `build_frame` produces both the training table and the live row.
"""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd

from ..db import get_conn

# Feature order is part of the persisted model - never reorder without a refit.
FEATURES = [
    "vix", "vix_ts", "dist_ma20", "rng_ma5", "prior_range_pct",
    "atr14_pct", "vol_5d", "vol_ratio", "net_ma5", "er_ma5", "er_ma20",
]

FEATURE_LABELS = {
    "vix": "VIX level",
    "vix_ts": "VIX term structure",
    "dist_ma20": "Distance from 20-day mean",
    "rng_ma5": "5-day mean range",
    "prior_range_pct": "Prior-day range",
    "atr14_pct": "ATR(14)",
    "vol_5d": "5-day volatility",
    "vol_ratio": "Short vs long vol",
    "net_ma5": "5-day mean net move",
    "er_ma5": "5-day efficiency regime",
    "er_ma20": "20-day efficiency regime",
}

# Efficiency is a ratio in [0,1]; a linear model can leave that range on extreme
# inputs, which would make the product meaningless. Clip to a plausible band.
_ER_FLOOR, _ER_CEIL = 0.05, 0.95


# --------------------------------------------------------------------------- #
# Feature construction
# --------------------------------------------------------------------------- #
def _daily_targets(d: pd.DataFrame) -> pd.DataFrame:
    """Range %, net move % and the daily efficiency proxy for each session.

    Yahoo intermittently prints an Open or Close outside the day's High/Low on
    ^GSPC. Such a bar would clamp the efficiency proxy to a perfect 1.00 - a broken
    bar reading as the cleanest possible session - so reject it and let that row
    drop out of the fit instead (same guard as factors._daily_bar_er).
    """
    out = pd.DataFrame(index=d.index)
    o, h, lo, c = d["Open"], d["High"], d["Low"], d["Close"]
    out["range_pct"] = (h - lo) / o * 100.0
    out["net_pct"] = (c - o).abs() / o * 100.0
    sane = (lo <= o) & (o <= h) & (lo <= c) & (c <= h) & (h > lo)
    out["er_day"] = np.where(sane, (c - o).abs() / (h - lo), np.nan)
    out.loc[~sane, ["range_pct", "net_pct"]] = np.nan
    return out


def build_frame(daily: pd.DataFrame, vix: pd.DataFrame,
                vix3m: pd.DataFrame | None = None) -> pd.DataFrame:
    """Features + targets per session, indexed by date.

    Every feature is shifted so it only ever sees COMPLETED sessions: the row for
    day D is scoreable at D's open. `build_frame` is used for both training and
    live scoring so the two can never drift apart.
    """
    if daily is None or daily.empty:
        return pd.DataFrame()

    d = daily.copy()
    d.index = pd.DatetimeIndex(d.index).tz_localize(None).normalize()
    d = d[~d.index.duplicated(keep="last")].sort_index()

    t = _daily_targets(d)
    ret = d["Close"].pct_change()
    true_range = pd.concat([
        d["High"] - d["Low"],
        (d["High"] - d["Close"].shift()).abs(),
        (d["Low"] - d["Close"].shift()).abs(),
    ], axis=1).max(axis=1)

    def _align(src: pd.DataFrame | None) -> pd.Series:
        if src is None or src.empty:
            return pd.Series(np.nan, index=d.index)
        s = src.copy()
        s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
        s = s[~s.index.duplicated(keep="last")].sort_index()
        return s["Close"].reindex(d.index).ffill()

    vix_c, vix3m_c = _align(vix), _align(vix3m)

    f = pd.DataFrame(index=d.index)
    f["vix"] = vix_c.shift(1)
    f["vix_ts"] = (vix_c - vix3m_c).shift(1)
    f["dist_ma20"] = ((d["Close"] / d["Close"].rolling(20).mean() - 1) * 100).shift(1)
    f["rng_ma5"] = t["range_pct"].shift(1).rolling(5).mean()
    f["prior_range_pct"] = t["range_pct"].shift(1)
    f["atr14_pct"] = (true_range.rolling(14).mean() / d["Close"] * 100).shift(1)
    f["vol_5d"] = (ret.rolling(5).std() * 100).shift(1)
    f["vol_ratio"] = (ret.rolling(5).std() / ret.rolling(20).std()).shift(1)
    f["net_ma5"] = t["net_pct"].shift(1).rolling(5).mean()
    f["er_ma5"] = t["er_day"].shift(1).rolling(5).mean()
    f["er_ma20"] = t["er_day"].shift(1).rolling(20).mean()

    # vix_ts is the only feature that can be entirely absent (VIX3M is optional and
    # already treated as degraded-not-fatal by the health check). Neutralise it to 0
    # rather than dropping every row, which would empty the training set.
    if f["vix_ts"].isna().all():
        f["vix_ts"] = 0.0

    return pd.concat([f, t], axis=1)


# --------------------------------------------------------------------------- #
# Ridge fit (closed form - no sklearn needed at predict time)
# --------------------------------------------------------------------------- #
def _fit_ridge(X: np.ndarray, y: np.ndarray, lam: float) -> dict:
    """Standardised ridge, solved directly. The intercept is left unpenalised."""
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd = np.where(sd == 0, 1.0, sd)
    z = np.c_[np.ones(len(X)), (X - mu) / sd]
    a = z.T @ z + lam * np.eye(z.shape[1])
    a[0, 0] -= lam
    w = np.linalg.solve(a, z.T @ y)
    return {"mu": mu.tolist(), "sd": sd.tolist(), "w": w.tolist()}


def _apply(leg: dict, X: np.ndarray) -> np.ndarray:
    mu = np.asarray(leg["mu"], dtype=float)
    sd = np.asarray(leg["sd"], dtype=float)
    w = np.asarray(leg["w"], dtype=float)
    z = np.c_[np.ones(len(X)), (X - mu) / sd]
    return z @ w


def _predict_net(model: dict, X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """-> (expected_net_pct, expected_range_pct, expected_efficiency)."""
    exp_range = np.exp(_apply(model["range_leg"], X))
    exp_er = np.clip(_apply(model["er_leg"], X), _ER_FLOOR, _ER_CEIL)
    return exp_range * exp_er, exp_range, exp_er


def fit(frame: pd.DataFrame, cfg_t: dict) -> dict:
    """Fit both legs and capture the trailing reference distribution.

    Returns a JSON-serialisable model. Raises ValueError when there is not enough
    clean history to fit - the caller keeps the previous model rather than
    installing a bad one.
    """
    lam = float(cfg_t.get("ridge_lambda", 10.0))
    window = int(cfg_t.get("reference_window", 500))
    min_samples = int(cfg_t.get("min_samples", 400))

    cols = FEATURES + ["range_pct", "net_pct", "er_day"]
    data = frame[cols].replace([np.inf, -np.inf], np.nan).dropna()
    if len(data) < min_samples:
        raise ValueError(f"only {len(data)} usable sessions, need {min_samples}")

    X = data[FEATURES].values.astype(float)
    model = {
        "features": list(FEATURES),
        "range_leg": _fit_ridge(X, np.log(data["range_pct"].values.astype(float)), lam),
        "er_leg": _fit_ridge(X, data["er_day"].values.astype(float), lam),
        "n_samples": int(len(data)),
        "date_from": str(data.index[0].date()),
        "date_to": str(data.index[-1].date()),
        "reference_window": window,
    }
    # The reference is the trailing window of REALISED net moves - what days actually
    # look like - so the score reads as "we expect roughly an Nth-percentile day".
    # Ranking against past PREDICTIONS instead is a trap: forecasts cluster far more
    # tightly than outcomes, so an ordinary quiet forecast lands at percentile 1. See
    # the module docstring.
    model["reference"] = sorted(float(v) for v in data["net_pct"].values[-window:])
    return model


def score_from_prediction(model: dict, expected_net: float) -> int:
    """Where a forecast sits in the realised-outcome distribution -> 1..100."""
    ref = model.get("reference") or []
    if not ref:
        return 50
    pos = int(np.searchsorted(np.asarray(ref, dtype=float), float(expected_net)))
    return int(np.clip(round(100.0 * pos / len(ref)), 1, 100))


def score_row(model: dict, row: pd.Series) -> dict | None:
    """Score one prepared feature row. None when any feature is missing."""
    try:
        X = np.array([[float(row[f]) for f in model["features"]]], dtype=float)
    except (KeyError, TypeError, ValueError):
        return None
    if not np.isfinite(X).all():
        return None

    net, rng, er = _predict_net(model, X)
    expected_net = float(net[0])
    return {
        "score": score_from_prediction(model, expected_net),
        "expected_net_pct": round(expected_net, 4),
        "expected_range_pct": round(float(rng[0]), 4),
        "expected_efficiency": round(float(er[0]), 4),
        "model_date_to": model.get("date_to"),
        "n_samples": model.get("n_samples"),
    }


def legs_breakdown(scored: dict) -> list[dict]:
    """Render the two legs in the same shape the UI already uses for factors."""
    if not scored:
        return []
    return [
        {
            "name": "expected_range",
            "label": "Expected range",
            "value": scored["expected_range_pct"],
            "unit": "%",
            "detail": f"model expects a {scored['expected_range_pct']:.2f}% high-low range",
            "strength": "strong",
        },
        {
            "name": "expected_efficiency",
            "label": "Expected efficiency",
            "value": scored["expected_efficiency"],
            "unit": "",
            "detail": (f"model expects {scored['expected_efficiency']:.2f} of that range "
                       "to be kept as net move"),
            "strength": "weak",
        },
        {
            "name": "expected_net_move",
            "label": "Expected net move",
            "value": scored["expected_net_pct"],
            "unit": "%",
            "detail": f"range x efficiency = {scored['expected_net_pct']:.2f}% expected travel",
            "strength": "derived",
        },
    ]


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def save_model(model: dict, notes: str = "") -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO tradeability_model (created_at, n_samples, date_from, date_to,"
            " payload_json, notes) VALUES (datetime('now'), ?, ?, ?, ?, ?)",
            (model.get("n_samples"), model.get("date_from"), model.get("date_to"),
             json.dumps(model), notes),
        )
        conn.commit()
    finally:
        conn.close()


def load_model() -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT payload_json, created_at FROM tradeability_model"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        model = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return None
    model["fitted_at"] = row["created_at"]
    return model


def model_age_days() -> float | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT julianday('now') - julianday(created_at) AS age"
            " FROM tradeability_model ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return float(row["age"]) if row and row["age"] is not None else None


# --------------------------------------------------------------------------- #
# Live path
# --------------------------------------------------------------------------- #
def _history(cfg: dict, price) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame | None]:
    t = cfg["tickers"]
    days = int(cfg.get("tradeability", {}).get("history_days", 2600))
    daily = price.daily_history(t["primary"], lookback_days=days)
    vix = price.daily_history(t["vix"], lookback_days=days)
    try:
        vix3m = price.daily_history(t["vix3m"], lookback_days=days)
    except Exception:  # noqa: BLE001 - VIX3M is optional (health check treats it as degraded)
        vix3m = None
    return daily, vix, vix3m


def refit(cfg: dict, price) -> dict:
    """Rebuild the model from upstream history and store it."""
    cfg_t = cfg.get("tradeability", {})
    daily, vix, vix3m = _history(cfg, price)
    frame = build_frame(daily, vix, vix3m)
    if frame.empty:
        return {"ok": False, "error": "no daily history"}
    try:
        model = fit(frame, cfg_t)
    except (ValueError, np.linalg.LinAlgError) as exc:
        return {"ok": False, "error": str(exc)}
    save_model(model, notes="scheduled refit")
    return {"ok": True, "n_samples": model["n_samples"],
            "date_from": model["date_from"], "date_to": model["date_to"]}


def ensure_model(cfg: dict, price) -> dict:
    """Refit when there is no model or the stored one has gone stale."""
    cfg_t = cfg.get("tradeability", {})
    age = model_age_days()
    if age is not None and age < float(cfg_t.get("refit_days", 7)):
        return {"ok": True, "refit": False}
    result = refit(cfg, price)
    result["refit"] = True
    # A failed refit is not fatal while a previous model is still on disk: an
    # upstream outage must not take the score offline, it just ages.
    if not result.get("ok") and age is not None:
        result["ok"] = True
        result["stale_model_kept"] = True
    return result


def score_day(cfg: dict, price, d: date) -> dict | None:
    """The live 09:30 read. None if the engine cannot produce a number."""
    model = load_model()
    if model is None:
        return None
    daily, vix, vix3m = _history(cfg, price)
    frame = build_frame(daily, vix, vix3m)
    if frame.empty:
        return None

    stamp = pd.Timestamp(d).normalize()
    if stamp in frame.index:
        row = frame.loc[stamp]
    else:
        # The session's own daily bar has not printed yet (normal at 09:30). Every
        # feature is built from completed sessions, so the last available row carries
        # exactly the inputs this day would use.
        prior = frame[frame.index < stamp]
        if prior.empty:
            return None
        row = prior.iloc[-1]

    scored = score_row(model, row)
    if scored is None:
        return None
    scored["band"] = band_for(cfg, scored["score"])
    scored["fitted_at"] = model.get("fitted_at")
    scored["legs"] = legs_breakdown(scored)
    return scored


def band_for(cfg: dict, score: int) -> str:
    """thin | normal | best - the reading a user acts on."""
    t = cfg.get("tradeability", {})
    if score >= int(t.get("band_good", 60)):
        return "best"
    if score <= int(t.get("band_caution", 38)):
        return "thin"
    return "normal"
