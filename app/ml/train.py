"""Train + calibrate the chop-vs-direction model.

Regresses the session Efficiency Ratio on as-of-prior-close features using a
HistGradientBoosting regressor, with a strict chronological hold-out (no shuffle
-> no lookahead). Saves a model bundle and records metrics in `model_versions`.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from ..config import get_config
from ..db import DATA_DIR, get_conn
from ..timeutils import now_et
from .dataset import build_dataset, load_dataset
from .features import FEATURE_COLUMNS, NEWS_FEATURE_COLUMNS

MIN_ROWS = 80


def _model():
    from sklearn.ensemble import HistGradientBoostingRegressor

    return HistGradientBoostingRegressor(
        loss="squared_error", learning_rate=0.05, max_iter=300,
        max_leaf_nodes=15, min_samples_leaf=30, l2_regularization=1.0,
        early_stopping=False, random_state=0,
    )


def _spearman(a, b):
    from scipy.stats import spearmanr

    val = spearmanr(a, b).correlation
    return None if val is None or np.isnan(val) else round(float(val), 3)


def train(cfg: dict | None = None, df: pd.DataFrame | None = None) -> dict:
    from sklearn.inspection import permutation_importance
    from sklearn.metrics import mean_absolute_error

    cfg = cfg or get_config()
    if df is None:
        df = load_dataset()
        if df.empty:
            df = build_dataset(cfg)
    if df.empty or len(df) < MIN_ROWS:
        return {"error": f"not enough data ({0 if df.empty else len(df)} rows); need >= {MIN_ROWS}"}

    df = df.sort_values("date").reset_index(drop=True)
    # Use base features + any news features that are present and not all-empty
    # (so news only enters the model after the backfill has populated them).
    feat_cols = [c for c in (FEATURE_COLUMNS + NEWS_FEATURE_COLUMNS)
                 if c in df.columns and df[c].notna().any()]
    X = df[feat_cols]
    y = df["session_er"].astype(float)
    n = len(df)
    split = int(n * (1 - cfg["ml"].get("test_fraction", 0.25)))
    Xtr, Xte, ytr, yte = X.iloc[:split], X.iloc[split:], y.iloc[:split], y.iloc[split:]

    model = _model()
    model.fit(Xtr, ytr)
    pred = model.predict(Xte)

    # Lift: realized ER of the model's top third vs bottom third on the hold-out.
    te = pd.DataFrame({"pred": pred, "real": yte.to_numpy()})
    q1, q2 = te["pred"].quantile([1 / 3, 2 / 3])
    top = te.loc[te["pred"] >= q2, "real"].mean()
    bot = te.loc[te["pred"] <= q1, "real"].mean()

    perm = permutation_importance(model, Xte, yte, n_repeats=10, random_state=0)
    importances = {f: float(v) for f, v in zip(feat_cols, perm.importances_mean)}

    metrics = {
        "spearman": _spearman(pred, yte),
        "baseline_spearman_prior_er": _spearman(Xte["prior_er"].fillna(0), yte),
        "mae": round(float(mean_absolute_error(yte, pred)), 4),
        "lift_top_third_er": None if np.isnan(top) else round(float(top), 3),
        "lift_bottom_third_er": None if np.isnan(bot) else round(float(bot), 3),
        "test_samples": int(len(yte)),
    }

    # Final model on all rows + percentile map (in-sample preds) -> 0..100 score.
    final = _model()
    final.fit(X, y)
    quantiles = np.percentile(final.predict(X), np.arange(0, 101)).tolist()

    bundle = {
        "model": final, "features": feat_cols, "pred_quantiles": quantiles,
        "importances": importances, "metrics": metrics,
        "created_at": now_et().isoformat(timespec="seconds"),
        "date_from": str(df["date"].min()), "date_to": str(df["date"].max()),
        "n_samples": int(n),
    }
    import joblib

    model_path = DATA_DIR / cfg["ml"]["model_file"]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, model_path)
    _record(bundle)

    return {"model_path": str(model_path), "n_samples": n,
            "date_from": bundle["date_from"], "date_to": bundle["date_to"],
            "importances": importances, **metrics}


def _record(bundle: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO model_versions (created_at, n_samples, date_from, date_to, "
            "metrics_json, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (bundle["created_at"], bundle["n_samples"], bundle["date_from"],
             bundle["date_to"], json.dumps(bundle["metrics"]),
             "HistGradientBoostingRegressor on session ER"),
        )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    r = train()
    if r.get("error"):
        print("[train] " + r["error"])
        return
    print(f"[train] n={r['n_samples']} sessions ({r['date_from']} .. {r['date_to']})")
    print(f"[train] Spearman {r['spearman']} (baseline prior_er {r['baseline_spearman_prior_er']}), "
          f"MAE {r['mae']}")
    print(f"[train] held-out realized ER: top-third {r['lift_top_third_er']} "
          f"vs bottom-third {r['lift_bottom_third_er']}")
    top = sorted(r["importances"].items(), key=lambda x: -x[1])[:6]
    print("[train] top features: " + ", ".join(f"{k} {v:.4f}" for k, v in top))


if __name__ == "__main__":
    main()
