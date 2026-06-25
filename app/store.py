"""Read helpers for the web UI (predictions, outcomes, accuracy)."""
from __future__ import annotations

import json

from .config import get_config
from .db import get_conn, init_db


def _row_to_pred(row) -> dict:
    d = dict(row)
    try:
        d["features"] = json.loads(d.get("features_json") or "{}")
    except json.JSONDecodeError:
        d["features"] = {}
    return d


def latest_prediction() -> dict | None:
    init_db()
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM predictions ORDER BY date DESC LIMIT 1"
        ).fetchone()
        return _row_to_pred(row) if row else None
    finally:
        conn.close()


def prediction_for(date_str: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM predictions WHERE date=?", (date_str,)
        ).fetchone()
        return _row_to_pred(row) if row else None
    finally:
        conn.close()


def recent_history(limit: int = 40) -> list[dict]:
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT p.date, p.tier, p.direction_quality, p.verdict, p.reason,
                   o.realized_er, o.realized_label, o.range_pct
            FROM predictions p
            LEFT JOIN outcomes o ON o.date = p.date
            ORDER BY p.date DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def accuracy_summary() -> dict:
    """Simple Phase-1 separation metrics (full ML calibration comes in Phase 3)."""
    cfg = get_config()
    good, caution = cfg["thresholds"]["good"], cfg["thresholds"]["caution"]
    rows = [r for r in recent_history(limit=10000) if r["realized_label"] is not None]

    summary = {
        "samples": len(rows), "good_threshold": good, "caution_threshold": caution,
        "avg_er_good": None, "avg_er_avoid": None,
        "veto_days": 0, "veto_chop_rate": None,
    }
    if not rows:
        return summary

    good_er = [r["realized_er"] for r in rows
               if r["tier"] not in ("VETO", "CLOSED")
               and r["direction_quality"] is not None
               and r["direction_quality"] >= good]
    avoid_er = [r["realized_er"] for r in rows
                if r["tier"] not in ("VETO", "CLOSED")
                and r["direction_quality"] is not None
                and r["direction_quality"] < caution]
    veto = [r for r in rows if r["tier"] == "VETO"]

    if good_er:
        summary["avg_er_good"] = round(sum(good_er) / len(good_er), 3)
    if avoid_er:
        summary["avg_er_avoid"] = round(sum(avoid_er) / len(avoid_er), 3)
    summary["veto_days"] = len(veto)
    if veto:
        chop = sum(1 for r in veto if r["realized_label"] == "CHOPPY")
        summary["veto_chop_rate"] = round(chop / len(veto), 3)
    return summary
