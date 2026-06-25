"""Free market/geopolitics headlines via GDELT's DOC 2.0 API (no key required).

GDELT is strong on global events (wars, sanctions, politics). Behind the
NewsProvider interface so a paid feed (Marketaux/Benzinga/etc.) can be dropped in.
"""
from __future__ import annotations

from datetime import date, timedelta

import requests

from ..timeutils import today_et
from .base import NewsProvider

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_HEADERS = {"User-Agent": "Mozilla/5.0 (TradeScale Phase4)"}


class GDELTNewsProvider(NewsProvider):
    def __init__(self, cfg: dict, timeout: int = 20):
        n = cfg["news"]
        self.query = n.get("query", "stock market")
        self.max_headlines = int(n.get("max_headlines", 25))
        self.timeout = timeout

    def headlines(self, d: date) -> list[str]:
        params = {
            "query": self.query, "mode": "artlist", "format": "json",
            "maxrecords": self.max_headlines, "sort": "datedesc",
        }
        if d >= today_et():
            params["timespan"] = "24h"
        else:  # historical day (UTC window)
            params["startdatetime"] = d.strftime("%Y%m%d000000")
            params["enddatetime"] = (d + timedelta(days=1)).strftime("%Y%m%d000000")

        resp = requests.get(GDELT_URL, params=params, headers=_HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            return []  # GDELT occasionally returns non-JSON on odd queries

        seen, titles = set(), []
        for art in data.get("articles", []):
            t = (art.get("title") or "").strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                titles.append(t)
        return titles[: self.max_headlines]
