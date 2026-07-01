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


def latest_model() -> dict | None:
    init_db()
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM model_versions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["metrics"] = json.loads(d.get("metrics_json") or "{}")
        except json.JSONDecodeError:
            d["metrics"] = {}
        return d
    finally:
        conn.close()


def accuracy_summary() -> dict:
    """Phase-1 track record: how often the tool's call matched the realized day.

    A GO call (score >= good, not vetoed) is "correct" when the day trended
    (DIRECTIONAL); an AVOID call (score < caution) is "correct" when it chopped
    (CHOPPY). The headline win rate is correct calls over all such decisive calls;
    the middle "be selective" band is not counted. VETO is tracked separately.
    """
    cfg = get_config()
    good, caution = cfg["thresholds"]["good"], cfg["thresholds"]["caution"]
    rows = [r for r in recent_history(limit=10000) if r["realized_label"] is not None]

    summary = {
        "samples": len(rows), "good_threshold": good, "caution_threshold": caution,
        "avg_er_good": None, "avg_er_avoid": None,
        "veto_days": 0, "veto_chop_rate": None,
        "go_n": 0, "go_win_rate": None,
        "avoid_n": 0, "avoid_hit_rate": None,
        "overall_n": 0, "overall_win_rate": None,
    }
    if not rows:
        return summary

    def _tradeable(r):
        return r["tier"] not in ("VETO", "CLOSED") and r["direction_quality"] is not None

    go = [r for r in rows if _tradeable(r) and r["direction_quality"] >= good]
    avoid = [r for r in rows if _tradeable(r) and r["direction_quality"] < caution]
    veto = [r for r in rows if r["tier"] == "VETO"]

    if go:
        summary["avg_er_good"] = round(sum(r["realized_er"] for r in go) / len(go), 3)
    if avoid:
        summary["avg_er_avoid"] = round(sum(r["realized_er"] for r in avoid) / len(avoid), 3)
    summary["veto_days"] = len(veto)
    if veto:
        summary["veto_chop_rate"] = round(
            sum(1 for r in veto if r["realized_label"] == "CHOPPY") / len(veto), 3)

    go_hits = sum(1 for r in go if r["realized_label"] == "DIRECTIONAL")
    avoid_hits = sum(1 for r in avoid if r["realized_label"] == "CHOPPY")
    summary["go_n"], summary["avoid_n"] = len(go), len(avoid)
    if go:
        summary["go_win_rate"] = round(go_hits / len(go), 3)
    if avoid:
        summary["avoid_hit_rate"] = round(avoid_hits / len(avoid), 3)

    decisive = len(go) + len(avoid)
    if decisive:
        summary["overall_n"] = decisive
        summary["overall_win_rate"] = round((go_hits + avoid_hits) / decisive, 3)
    return summary
