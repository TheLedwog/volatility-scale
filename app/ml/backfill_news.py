"""Score ~2 years of historical news with GPT, add news model features, retrain.

Safe by default: prints a cost estimate and does NOTHING until you pass --yes.
It's cached and resumable (already-scored days are skipped, so re-runs don't
re-bill), and it throttles GDELT to stay under its rate limit.

    python -m app.ml.backfill_news          # dry run: estimate only
    python -m app.ml.backfill_news --yes    # actually run it
"""
from __future__ import annotations

import datetime as dt
import sys
import time

from ..config import get_config, openai_api_key
from .dataset import TRAINING_CSV, build_dataset, load_dataset
from .features import NEWS_FEATURE_COLUMNS, news_feature_dict
from .train import train

# Rough per-call token usage for the estimate (system prompt + ~25 headlines in,
# small JSON out). Approximate — check your OpenAI dashboard for real figures.
_TOK_IN, _TOK_OUT = 1000, 120
_PRICES = {  # USD per 1M tokens (approximate)
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.0),
}


def _estimate_usd(n: int, model: str) -> float:
    cin, cout = _PRICES.get(model, _PRICES["gpt-4o-mini"])
    return n * (_TOK_IN / 1e6 * cin + _TOK_OUT / 1e6 * cout)


def run(confirm: bool = False) -> dict:
    from ..scoring.news import get_news_assessment

    cfg = get_config()
    if not cfg["news"].get("enabled", False):
        print("[backfill] news is disabled — set news.enabled = true in Settings.")
        return {"error": "news disabled"}
    if not openai_api_key(cfg):
        print("[backfill] no OpenAI key found. Set OPENAI_API_KEY (env/.env) or "
              "providers.openai_api_key in Settings.")
        return {"error": "no key"}

    df = load_dataset()
    if df.empty:
        print("[backfill] building base dataset first...")
        df = build_dataset(cfg)
    if df.empty:
        print("[backfill] no dataset available.")
        return {"error": "no dataset"}

    dates = list(df["date"])
    model = cfg["providers"].get("openai_model", "gpt-4o-mini")
    est = _estimate_usd(len(dates), model)
    print(f"[backfill] {len(dates)} sessions to score with '{model}'.")
    print(f"[backfill] rough one-time estimate ~${est:.2f} (cached & resumable; "
          f"already-scored days are skipped).")
    if not confirm:
        print("[backfill] DRY RUN - nothing called. Re-run to proceed:")
        print("           python -m app.ml.backfill_news --yes")
        return {"dry_run": True, "sessions": len(dates), "estimate_usd": round(est, 2)}

    feats = {c: [] for c in NEWS_FEATURE_COLUMNS}
    scored = 0
    for i, ds in enumerate(dates, 1):
        d = dt.date.fromisoformat(ds)
        try:
            a = get_news_assessment(cfg, d)
        except Exception as exc:  # noqa: BLE001
            a = None
            print(f"[backfill] {ds}: {exc}")
        nf = news_feature_dict(a)
        for c in NEWS_FEATURE_COLUMNS:
            feats[c].append(nf[c])
        if a and a.get("scored"):
            scored += 1
        if i % 25 == 0:
            print(f"[backfill] {i}/{len(dates)} (scored {scored})")
        time.sleep(1.0)  # be polite to GDELT's rate limit

    for c in NEWS_FEATURE_COLUMNS:
        df[c] = feats[c]
    df.to_csv(TRAINING_CSV, index=False)
    print(f"[backfill] news features added for {scored}/{len(dates)} sessions. Retraining...")

    r = train(df=df)
    if r.get("error"):
        print("[backfill] " + r["error"])
        return r
    print(f"[backfill] done. Spearman {r['spearman']} (price-only was ~0.02); "
          f"features used: {len(r.get('importances', {}))}")
    used_news = [c for c in NEWS_FEATURE_COLUMNS if c in r.get("importances", {})]
    print(f"[backfill] news features in model: {used_news or 'none'}")
    return r


def main() -> None:
    run(confirm="--yes" in sys.argv)


if __name__ == "__main__":
    main()
