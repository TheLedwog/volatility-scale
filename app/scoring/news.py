"""Fetch + GPT-score the day's news, with a per-day DB cache.

One GPT call per day (cached), so re-running a prediction won't re-bill. Scores
are only cached once actually GPT-scored, so adding a key later triggers a fresh call.
"""
from __future__ import annotations

import json
from datetime import date

from ..config import openai_api_key
from ..db import get_conn
from ..providers import get_llm_provider, get_news_provider
from ..timeutils import now_et


def _load(date_str: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM news_scores WHERE date=?", (date_str,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["headlines"] = json.loads(d.get("headlines_json") or "[]")
    except json.JSONDecodeError:
        d["headlines"] = []
    d["scored"] = bool(d.get("scored"))
    return d


def _save(a: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO news_scores
                (date, scored, relevance, expected_impact, direction, chop_risk,
                 rationale, source, headlines_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                scored=excluded.scored, relevance=excluded.relevance,
                expected_impact=excluded.expected_impact, direction=excluded.direction,
                chop_risk=excluded.chop_risk, rationale=excluded.rationale,
                source=excluded.source, headlines_json=excluded.headlines_json,
                created_at=excluded.created_at
            """,
            (a["date"], int(bool(a.get("scored"))), a.get("relevance"),
             a.get("expected_impact"), a.get("direction"), a.get("chop_risk"),
             a.get("rationale"), a.get("source"),
             json.dumps(a.get("headlines", [])), now_et().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def get_news_assessment(cfg: dict, d: date, use_cache: bool = True) -> dict | None:
    if not cfg.get("news", {}).get("enabled", False):
        return None
    date_str = d.isoformat()
    has_key = bool(openai_api_key(cfg))

    if use_cache:
        cached = _load(date_str)
        if cached:
            # Reuse if already GPT-scored, or if we still have no key to score with
            # (avoids re-hitting GDELT every run). Re-score once a key is added.
            if cached["scored"] or not has_key:
                return cached

    fetch_err = None
    try:
        headlines = get_news_provider(cfg).headlines(d)
    except Exception as exc:  # noqa: BLE001
        headlines, fetch_err = [], str(exc)

    score = get_llm_provider(cfg).score_news(headlines, cfg) if headlines else {"scored": False}

    assessment = {
        "date": date_str,
        "headlines": headlines,
        "scored": bool(score.get("scored")),
        "relevance": score.get("relevance"),
        "expected_impact": score.get("expected_impact"),
        "direction": score.get("direction"),
        "chop_risk": score.get("chop_risk"),
        "rationale": score.get("rationale"),
        "source": cfg["news"].get("provider", "gdelt"),
        "error": score.get("error") or fetch_err or (None if headlines else "no headlines"),
    }
    # Cache headlines too (not just scored results) so repeated runs don't re-hit GDELT.
    if headlines or assessment["scored"]:
        _save(assessment)
    return assessment
