"""Placeholder news provider (Phase 4 will wire GDELT + financial RSS)."""
from __future__ import annotations

from datetime import date

from .base import NewsProvider


class StubNewsProvider(NewsProvider):
    def headlines(self, d: date) -> list[str]:
        return []
