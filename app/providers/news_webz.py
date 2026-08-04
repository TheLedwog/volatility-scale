"""Market/geopolitics headlines via Webz.io's News API (token-authenticated).

GDELT rate-limits by IP, and a deployed host shares its IP reputation with every
other tenant - which is how a 429 at 09:30 blanked the news factor (25% of the
scoring weight) for a whole session. A token-authenticated quota is the actual
fix rather than retry-and-hope: it is ours alone, and a noisy neighbour on the
same host cannot spend it.

Free tier is 500 calls / 5,000 results a month at 10 results per call, so a full
headline set is paginated and the call budget is deliberate: `webz_max_calls`
caps how many pages one fetch will pull. At ~21 trading days that is well inside
the quota even at the cap. The API allows 1 request/sec per token, hence the
pace between pages.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import requests

from ..timeutils import today_et
from .base import NewsProvider, filter_market_headlines

WEBZ_URL = "https://api.webz.io/filterWebContent"
_RETRY_STATUS = (429, 500, 502, 503, 504)


class WebzNewsError(RuntimeError):
    """Raised so the failure reason reaches the card instead of a silent empty list."""


class WebzNewsProvider(NewsProvider):
    name = "webz"

    def __init__(self, cfg: dict, token: str, timeout: int = 20):
        n = cfg["news"]
        self.token = token
        self.query = n.get("webz_query") or ""
        self.max_headlines = int(n.get("max_headlines", 25))
        self.max_calls = max(1, int(n.get("webz_max_calls", 3)))
        self.page_size = max(1, int(n.get("webz_page_size", 10)))
        self.require_finance = bool(n.get("require_finance_terms", True))
        self.extra_terms = n.get("extra_finance_terms", [])
        self.timeout = timeout
        self.retries = max(1, int(n.get("fetch_retries", 3)))
        self.retry_backoff = float(n.get("fetch_retry_backoff_sec", 2.0))

    def _get(self, params: dict | None, url: str = WEBZ_URL):
        """GET with backoff. `next` pages come back as a ready-made URL, no params."""
        delay, last = self.retry_backoff, None
        for attempt in range(self.retries):
            last = requests.get(url, params=params, timeout=self.timeout)
            if last.status_code not in _RETRY_STATUS:
                return last
            if attempt < self.retries - 1:
                time.sleep(delay)
                delay *= 2
        return last

    def headlines(self, d: date) -> list[str]:
        # Crawl-time cutoff. Same 24h window the GDELT path used, so a swap doesn't
        # quietly change what "today's news" means.
        start = datetime.now(timezone.utc) - timedelta(hours=24)
        if d < today_et():  # historical backfill: that day's UTC window
            start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        params = {
            "token": self.token,
            "format": "json",
            "q": self.query,
            "ts": int(start.timestamp() * 1000),
            "sort": "relevancy",
            "size": self.page_size,
        }

        seen, titles, url, page_params = set(), [], WEBZ_URL, params
        for call in range(self.max_calls):
            if call:
                time.sleep(1.05)  # 1 request/sec per token
            resp = self._get(page_params, url)
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError as exc:
                raise WebzNewsError(f"non-JSON response: {exc}") from exc
            if data.get("error"):
                raise WebzNewsError(str(data["error"]))

            for post in data.get("posts", []) or []:
                t = ((post.get("title") or "").strip())
                if t and t.lower() not in seen:
                    seen.add(t.lower())
                    titles.append(t)

            nxt = data.get("next")
            if not nxt or len(titles) >= self.max_headlines * 2:
                break
            # `next` is a path relative to the API host, already carrying the token.
            url, page_params = (nxt if nxt.startswith("http") else f"https://api.webz.io{nxt}"), None

        if self.require_finance:
            titles = filter_market_headlines(titles, self.extra_terms)
        return titles[: self.max_headlines]
