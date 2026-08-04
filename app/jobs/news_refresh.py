"""Warm the day's news read before the open, so 09:30 doesn't depend on one fetch.

Same shape as the calendar cache: the gate learned this lesson already - a single
upstream call at the exact moment you need it will eventually fail, and a factor
that silently drops out is worse than one that is late. GDELT rate-limits its free
endpoint, so on 2026-08-04 a 429 at 09:30 blanked the news factor (25% of the
scoring weight) for the whole session.

Each run is a no-op once the day is cached and scored, so scheduling several
attempts before the open costs at most one successful upstream call per day.
"""
from __future__ import annotations

from datetime import date

from ..config import get_config
from ..market_calendar import is_trading_day
from ..scoring.news import get_news_assessment
from ..timeutils import today_et


def refresh_news(d: date | None = None) -> dict:
    """Fetch + score today's news into the cache. Safe to call repeatedly."""
    cfg = get_config()
    d = d or today_et()
    if not cfg.get("news", {}).get("enabled", False):
        return {"ok": False, "skipped": "news disabled"}
    if not is_trading_day(d):
        return {"ok": False, "skipped": "not a trading day"}

    try:
        a = get_news_assessment(cfg, d)
    except Exception as exc:  # noqa: BLE001 - a warm-up must never take the app down
        return {"ok": False, "date": d.isoformat(), "error": str(exc)}

    if not a:
        return {"ok": False, "date": d.isoformat(), "error": "news disabled"}
    return {
        "ok": bool(a.get("scored")),
        "date": d.isoformat(),
        "headlines": len(a.get("headlines") or []),
        "scored": bool(a.get("scored")),
        "error": a.get("error"),
    }


def main() -> None:
    print(f"[news] {refresh_news()}")


if __name__ == "__main__":
    main()
