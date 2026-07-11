"""On-demand health checks for the scraped data feeds (yfinance, ForexFactory).

Both are free, unofficial sources that can break silently when the upstream site
changes its markup/endpoints - which would quietly degrade the score instead of
erroring loudly. The Settings page runs these so a broken scrape is visible. Each
probe exercises the exact call the engine/labeler depends on.
"""
from __future__ import annotations

from ..timeutils import today_et
from . import get_calendar_provider, get_price_provider


def _probe(name: str, ticker: str, fn, optional: bool = False) -> dict:
    """Run one check: fn() returns a detail string on success or raises on failure."""
    try:
        detail = fn()
        return {"name": name, "ticker": ticker, "ok": True, "optional": optional, "detail": detail}
    except Exception as exc:  # noqa: BLE001 - report any failure, don't crash the page
        return {"name": name, "ticker": ticker, "ok": False, "optional": optional,
                "detail": (str(exc)[:160] or type(exc).__name__)}


def check_price_feed(cfg: dict) -> dict:
    price = get_price_provider()
    t = cfg["tickers"]

    def daily():
        df = price.daily_history(t["primary"], lookback_days=7)
        if df is None or df.empty or "Close" not in df:
            raise ValueError("no rows / no Close column")
        close = df["Close"].dropna()
        if close.empty:
            raise ValueError("Close column empty")
        return f"{len(df)} rows, last close {close.iloc[-1]:.2f}"

    def level(tk):
        def _f():
            v = price.last_close(tk)
            if v is None:
                raise ValueError("no value returned")
            return f"last {v:.2f}"
        return _f

    def bars(tk):
        def _f():
            df = price.intraday(tk, interval="5m", lookback_days=2)
            if df is None or df.empty:
                raise ValueError("no bars returned")
            return f"{len(df)} 5-min bars"
        return _f

    checks = [
        _probe("Daily history", t["primary"], daily),
        _probe("VIX level", t.get("vix"), level(t.get("vix"))),
        _probe("VIX3M level", t.get("vix3m"), level(t.get("vix3m")), optional=True),
        _probe("Intraday index bars", t["primary"], bars(t["primary"])),
        _probe("Overnight futures bars", t["futures"], bars(t["futures"])),
    ]
    required_ok = all(c["ok"] for c in checks if not c["optional"])
    return {"provider": "yfinance (prices / VIX / futures)", "ok": required_ok, "checks": checks}


def check_calendar_feed(cfg: dict) -> dict:
    name = cfg.get("providers", {}).get("calendar", "forexfactory")

    def _upstream():
        events = get_calendar_provider().events_week()
        return f"{len(events)} US events this week"

    def _cache():
        # What the GATE actually reads. The upstream probe can fail (FF rate-limits)
        # while the gate is still perfectly healthy off a recent cache - and vice versa,
        # a fresh feed is no use if nothing has written it to the cache.
        from ..calendar_store import events_for as cached_events_for
        from ..service.calendar_view import calendar_status

        st = calendar_status(cfg)
        if st["never_fetched"]:
            raise ValueError("calendar has never been fetched - the gate cannot see events")
        n = len(cached_events_for(today_et()))
        age = st["age_hours"]
        detail = f"{n} US events today, fetched {age:.1f}h ago"
        if st["stale"]:
            raise ValueError(f"cache is stale ({detail})")
        return detail

    checks = [
        _probe("Economic calendar (upstream)", name, _upstream, optional=True),
        _probe("Calendar cache (drives the gate)", "sqlite", _cache),
    ]
    required_ok = all(c["ok"] for c in checks if not c["optional"])
    return {"provider": "ForexFactory (calendar / VETO gate)",
            "ok": required_ok, "checks": checks}


def check_all_feeds(cfg: dict) -> dict:
    feeds = [check_price_feed(cfg), check_calendar_feed(cfg)]
    ok = all(f["ok"] for f in feeds)
    degraded = any(c.get("optional") and not c["ok"] for f in feeds for c in f["checks"])
    return {"ok": ok, "degraded": degraded, "feeds": feeds}
