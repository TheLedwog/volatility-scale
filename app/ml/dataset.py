"""Build the historical training table (features + session-ER label) from free data.

Daily/VIX history goes back ~years; the intraday label uses hourly bars (yfinance
caps hourly at ~730 days), so the dataset is roughly the last ~2 years of sessions.
As precise 5-min labels accumulate going forward, retraining sharpens it.
"""
from __future__ import annotations

import pandas as pd

from ..config import get_config
from ..db import DATA_DIR
from ..providers import get_price_provider
from .features import build_training_frame

TRAINING_CSV = DATA_DIR / "training.csv"


def build_dataset(cfg: dict | None = None, save: bool = True) -> pd.DataFrame:
    cfg = cfg or get_config()
    price = get_price_provider()
    t, ml = cfg["tickers"], cfg["ml"]

    daily = price.daily_history(t["primary"], lookback_days=ml["lookback_days_daily"])
    vix = price.daily_history(t["vix"], lookback_days=ml["lookback_days_daily"])
    try:
        vix3m = price.daily_history(t["vix3m"], lookback_days=ml["lookback_days_daily"])
    except Exception:  # noqa: BLE001
        vix3m = None
    hourly = price.intraday(t["primary"], interval="1h",
                            lookback_days=ml["lookback_days_hourly"])

    df = build_training_frame(daily, vix, vix3m, hourly, min_bars=ml["min_session_bars"])
    if save and not df.empty:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(TRAINING_CSV, index=False)
    return df


def load_dataset() -> pd.DataFrame:
    if TRAINING_CSV.exists():
        return pd.read_csv(TRAINING_CSV)
    return pd.DataFrame()


def main() -> None:
    df = build_dataset()
    print(f"[dataset] built {len(df)} rows -> {TRAINING_CSV}")
    if not df.empty:
        print(f"[dataset] sessions {df['date'].min()} .. {df['date'].max()}")
        print("[dataset] session_er:", df["session_er"].describe().round(3).to_dict())


if __name__ == "__main__":
    main()
