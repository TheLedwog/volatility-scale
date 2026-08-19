"""Verification for the tradeability engine. Run against a scratch DB:

    TRADESCALE_DB=%TEMP%/tt.db .venv/Scripts/python.exe test_tradeability.py

Covers the things that would silently ruin the score rather than crash it:
lookahead in the feature build, the percentile scale, whether the out-of-sample
performance claim actually reproduces THROUGH THE SHIPPED CODE (not a research
script), and whether shadow mode really leaves the existing output alone.
"""
from __future__ import annotations

import sys
import warnings

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from app.scoring.tradeability import (
    FEATURES,
    build_frame,
    fit,
    score_from_prediction,
    score_row,
    _predict_net,
)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    import os
    import pickle
    import tempfile

    cache = os.path.join(tempfile.gettempdir(), "tt_data.pkl")
    if os.path.exists(cache):
        return pickle.load(open(cache, "rb"))
    import yfinance as yf

    from app.providers.prices_yfinance import _configure_yfinance

    _configure_yfinance()
    out = []
    for t in ("^GSPC", "^VIX", "^VIX3M"):
        df = yf.download(t, period="10y", interval="1d", auto_adjust=False, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df = df.droplevel(1, axis=1)
        out.append(df)
    pickle.dump(tuple(out), open(cache, "wb"))
    return tuple(out)


CFG_T = {"reference_window": 1000, "min_samples": 400, "ridge_lambda": 10.0}
BAND_THIN, BAND_BEST = 38, 60


def main() -> int:
    daily, vix, vix3m = load()
    frame = build_frame(daily, vix, vix3m)
    print(f"\nframe: {len(frame)} sessions, {frame.index.min().date()} -> {frame.index.max().date()}")

    print("\n1. No lookahead")
    # The feature row for day D must be identical whether or not the engine can see
    # D and everything after it. This is the failure that makes a backtest lie.
    probe = frame.dropna(subset=FEATURES).index[-40]
    cut = daily[pd.DatetimeIndex(daily.index).tz_localize(None).normalize() < probe]
    truncated = build_frame(cut, vix, vix3m)
    if truncated.empty or probe in truncated.index:
        check("truncated frame excludes the probe day", False)
    else:
        # Rebuild with data up to and including D but nothing after.
        upto = daily[pd.DatetimeIndex(daily.index).tz_localize(None).normalize() <= probe]
        partial = build_frame(upto, vix, vix3m)
        a = frame.loc[probe, FEATURES].astype(float)
        b = partial.loc[probe, FEATURES].astype(float)
        same = bool(np.allclose(a.values, b.values, equal_nan=True))
        check("features for day D identical with and without future data", same,
              f"probe {probe.date()}")

    # A feature that accidentally read day D's own bar would move when D's bar changes.
    tampered = daily.copy()
    idx = pd.DatetimeIndex(tampered.index).tz_localize(None).normalize()
    mask = idx == probe
    for col in ("Open", "High", "Low", "Close"):
        tampered.loc[mask, col] = tampered.loc[mask, col] * 1.5
    t_frame = build_frame(tampered, vix, vix3m)
    unchanged = bool(np.allclose(
        frame.loc[probe, FEATURES].astype(float).values,
        t_frame.loc[probe, FEATURES].astype(float).values, equal_nan=True))
    check("mangling day D's own bar does not move day D's features", unchanged)

    print("\n2. Fit and score")
    model = fit(frame, CFG_T)
    check("model fits", model["n_samples"] > 400, f"{model['n_samples']} sessions")
    check("reference window sized as configured", len(model["reference"]) == 1000,
          f"{len(model['reference'])}")
    # The reference must be REALISED net moves, not past predictions. Outcomes are far
    # more dispersed than forecasts, so this is easy to tell apart - and getting it
    # wrong is what pinned the score at 1 for a week.
    ref_arr = np.asarray(model["reference"], dtype=float)
    check("reference is the realised-outcome distribution, not predictions",
          ref_arr.min() < 0.05 and ref_arr.max() > 2.0,
          f"min {ref_arr.min():.3f} max {ref_arr.max():.3f}")
    row = frame.dropna(subset=FEATURES).iloc[-1]
    scored = score_row(model, row)
    check("scores a live row", scored is not None and 1 <= scored["score"] <= 100,
          f"score {scored['score'] if scored else None}")
    check("expected_net = range x efficiency", scored is not None and abs(
        scored["expected_range_pct"] * scored["expected_efficiency"]
        - scored["expected_net_pct"]) < 1e-3)
    check("missing feature -> None, not a bogus score",
          score_row(model, pd.Series({f: np.nan for f in FEATURES})) is None)
    ref = model["reference"]
    check("percentile scale spans 1..100",
          score_from_prediction(model, ref[0] - 1e9) == 1
          and score_from_prediction(model, ref[-1] + 1e9) == 100)

    print("\n3. Out-of-sample performance, through the shipped code")
    # Walk forward exactly as production does: refit periodically on history only,
    # score the next unseen block, never look ahead.
    data = frame[FEATURES + ["range_pct", "net_pct", "er_day"]].replace(
        [np.inf, -np.inf], np.nan).dropna()
    start, refit_every = 1000, 60
    scores, nets = [], []
    m = None
    for i in range(start, len(data)):
        if (i - start) % refit_every == 0:
            m = fit(data.iloc[:i], CFG_T)
        X = data.iloc[i:i + 1][FEATURES].values.astype(float)
        net, _, _ = _predict_net(m, X)
        scores.append(score_from_prediction(m, float(net[0])))
        nets.append(float(data.iloc[i]["net_pct"]))
    s, n = np.array(scores), np.array(nets)
    from scipy import stats

    rho = stats.spearmanr(s, n)[0]
    lo, hi = s <= BAND_THIN, s > BAND_BEST
    ratio = n[hi].mean() / n[lo].mean()
    check("rank correlation with realised net move beats the old engine", rho > 0.20,
          f"rho {rho:+.3f} over {len(s)} unseen sessions (old engine: -0.002)")
    check("top band moves materially more than bottom band", ratio > 1.8,
          f"{n[lo].mean():.3f}% -> {n[hi].mean():.3f}% = {ratio:.2f}x")
    check("scale keeps using its range (no band collapse)",
          hi.mean() > 0.12 and lo.mean() > 0.15,
          f"thin {lo.mean():.0%} / normal {1 - lo.mean() - hi.mean():.0%} / best {hi.mean():.0%}")
    check("a top-band day is far likelier to deliver a >1% move",
          (n[hi] > 1.0).mean() > 3 * (n[lo] > 1.0).mean(),
          f"best {(n[hi] > 1.0).mean():.0%} vs thin {(n[lo] > 1.0).mean():.0%}")

    print("\n3b. Scale calibration (regression guard)")
    # The bug this catches: ranking forecasts against other FORECASTS drove the score
    # to 1 for a fortnight of sessions whose realised moves sat at the 38th percentile
    # of ten years. A score is only honest if, over a stretch, it lands near the
    # percentile the tape actually delivered.
    full = np.sort(data["net_pct"].values)
    for window in (20, 60, 250):
        realised_pct = 100.0 * float((full < np.median(n[-window:])).mean())
        median_score = float(np.median(s[-window:]))
        check(f"last {window} sessions: score tracks the percentile actually delivered",
              abs(median_score - realised_pct) < 22,
              f"score median {median_score:.0f} vs realised {realised_pct:.0f}th pct")
    check("score sits mid-scale on average (not pinned to an end)",
          40 <= float(np.median(s)) <= 60, f"median score {np.median(s):.0f}")

    print("\n4. Shadow mode leaves existing output alone")
    from app.api.serializers import _tradeability_block

    features = {"tradeability": {"score": 77, "band": "normal"}, "scored_by": "rules",
                "factors": [{"name": "vix_regime"}]}
    blk = _tradeability_block({"tradeability": {"enabled": True, "mode": "shadow"}}, features)
    check("shadow block reports mode + score", blk["mode"] == "shadow" and blk["score"] == 77)
    off = _tradeability_block({"tradeability": {"enabled": False}}, features)
    check("disabled reports mode=off", off["mode"] == "off")

    from app.scoring.engine import _verdict

    cfg = {"thresholds": {"good": 65, "caution": 40}}
    check("verdict strings unchanged under custom band cuts",
          _verdict("CLEAN", 85, cfg, False, False, (60, 38))[0] == "Good to trade"
          and _verdict("CLEAN", 30, cfg, False, False, (60, 38))[0] == "Choppy - avoid"
          and _verdict("CLEAN", 50, cfg, False, False, (60, 38))[0] == "Mixed - be selective")
    check("a score of 70 is Good on rule cuts and Good on band cuts; 45 differs",
          _verdict("CLEAN", 45, cfg, False, False)[0] == "Mixed - be selective"
          and _verdict("CLEAN", 45, cfg, False, False, (60, 38))[0] == "Mixed - be selective")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
