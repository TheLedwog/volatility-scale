"""Live scorer backed by the trained model bundle."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from ..config import get_config
from ..db import DATA_DIR
from .features import (
    FEATURE_LABELS, NEWS_FEATURE_COLUMNS, feature_row, news_feature_dict, to_date_index,
)


class ModelScorer:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or get_config()
        self.bundle = None
        path = DATA_DIR / self.cfg["ml"]["model_file"]
        if path.exists():
            try:
                import joblib

                self.bundle = joblib.load(path)
            except Exception:  # noqa: BLE001
                self.bundle = None

    def available(self) -> bool:
        return self.bundle is not None

    def is_useful(self) -> bool:
        """True only if the model beat near-random on its hold-out (gates 'auto')."""
        if not self.available():
            return False
        m = self.bundle.get("metrics", {}) or {}
        ml = self.cfg["ml"]
        sp, top, bot = m.get("spearman"), m.get("lift_top_third_er"), m.get("lift_bottom_third_er")
        ok_sp = sp is not None and sp >= ml.get("min_spearman", 0.05)
        ok_lift = top is not None and bot is not None and (top - bot) >= ml.get("min_lift", 0.02)
        return ok_sp and ok_lift

    def _dq(self, pred: float) -> int:
        q = np.asarray(self.bundle["pred_quantiles"], dtype=float)
        return int(max(0, min(100, int(np.searchsorted(q, pred)))))

    def score(self, cfg: dict, price, d: date, news: dict | None = None) -> dict | None:
        if not self.available():
            return None
        t = cfg["tickers"]
        daily = to_date_index(price.daily_history(t["primary"], 90))
        vix = to_date_index(price.daily_history(t["vix"], 90))
        try:
            vix3m = to_date_index(price.daily_history(t["vix3m"], 90))
        except Exception:  # noqa: BLE001
            vix3m = pd.DataFrame()

        row = feature_row(daily, vix, vix3m, d)
        if row is None:
            return None

        feat_cols = self.bundle.get("features", list(row.keys()))
        # Add live news features only if the trained model expects them.
        if any(c in feat_cols for c in NEWS_FEATURE_COLUMNS):
            row = {**row, **news_feature_dict(news)}

        X = pd.DataFrame([{k: row.get(k, np.nan) for k in feat_cols}])[feat_cols]
        pred = float(self.bundle["model"].predict(X)[0])
        dq = self._dq(pred)

        imps = self.bundle.get("importances", {})
        total = sum(abs(v) for v in imps.values()) or 1.0
        factors = []
        for name, imp in sorted(imps.items(), key=lambda x: -x[1])[:6]:
            val = row.get(name)
            shown = isinstance(val, (int, float)) and val == val  # not NaN
            factors.append({
                "name": name, "label": FEATURE_LABELS.get(name, name),
                "risk": round(max(0.0, imp) / total, 3),
                "weight": round(float(imp), 4),
                "contribution": round(max(0.0, imp) / total, 3),
                "available": shown,
                "detail": f"value: {val:.2f}" if shown else "n/a",
            })

        atr_pct = row.get("atr14_pct")
        dead = (isinstance(atr_pct, (int, float)) and atr_pct == atr_pct
                and atr_pct < cfg["thresholds"].get("dead_day_range_pct", 0.0))

        return {
            "factors": factors,
            "chop_risk": round(1 - dq / 100.0, 3),
            "direction_quality": dq,
            "dead_day": bool(dead),
            "atr_pct": atr_pct,
            "breakdown_kind": "model",
            "predicted_er": round(pred, 3),
            "model_version": self.bundle.get("created_at"),
        }
