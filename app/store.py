"""Read helpers for the web UI (predictions, outcomes, accuracy)."""
from __future__ import annotations

import json

from .config import get_config
from .db import get_conn, init_db
from .scoring.calibration import calibrate
from .service.verdict import overall_score


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


def realized_er_for(date_str: str) -> float | None:
    """The graded 5-min efficiency ratio for one session, if it has been labelled.

    Lets the prior-day factor use the same measurement the labeler grades against
    instead of a daily O/H/L/C proxy of it.
    """
    init_db()
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT realized_er FROM outcomes WHERE date=?", (date_str,)
        ).fetchone()
    finally:
        conn.close()
    return float(row["realized_er"]) if row and row["realized_er"] is not None else None


def recent_history(limit: int = 40) -> list[dict]:
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT p.date, p.tier, p.direction_quality, p.verdict, p.reason,
                   p.features_json, o.realized_er, o.realized_label, o.range_pct
            FROM predictions p
            LEFT JOIN outcomes o ON o.date = p.date
            ORDER BY p.date DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    # `overall` is the score the gauge actually SHOWED that day: direction_quality
    # with the VETO/WARN discount folded in (unchanged for CLEAN days). Computed here
    # so history reproduces the real needle for gated days, not just the raw score.
    cfg = get_config()
    cal = calibrate(cfg)
    out = []
    for r in rows:
        d = dict(r)
        try:
            features = json.loads(d.pop("features_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            d.pop("features_json", None)
            features = {}
        pred = {"tier": d["tier"], "direction_quality": d["direction_quality"],
                "features": features}
        d["overall"] = overall_score(pred, cfg, cal)
        out.append(d)
    return out


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


def _decisive_stats(scored: list[tuple[int, dict]], good: int, caution: int) -> dict:
    """Win rates for one scoring basis, given (score, row) pairs.

    A GO call (score >= good) is correct when the day trended (DIRECTIONAL); an
    AVOID call (score < caution) is correct when it chopped (CHOPPY). The middle
    "be selective" band is not a call, so it isn't counted either way.
    """
    go = [r for (s, r) in scored if s >= good]
    avoid = [r for (s, r) in scored if s < caution]
    go_hits = sum(1 for r in go if r["realized_label"] == "DIRECTIONAL")
    avoid_hits = sum(1 for r in avoid if r["realized_label"] == "CHOPPY")
    decisive = len(go) + len(avoid)
    return {
        "go_n": len(go),
        "go_win_rate": round(go_hits / len(go), 3) if go else None,
        "avoid_n": len(avoid),
        "avoid_hit_rate": round(avoid_hits / len(avoid), 3) if avoid else None,
        "overall_n": decisive,
        "overall_win_rate": round((go_hits + avoid_hits) / decisive, 3) if decisive else None,
        "avg_er_good": round(sum(r["realized_er"] for r in go) / len(go), 3) if go else None,
        "avg_er_avoid": round(sum(r["realized_er"] for r in avoid) / len(avoid), 3) if avoid else None,
    }


def accuracy_summary() -> dict:
    """Phase-1 track record: how often the tool's call matched the realized day.

    Graded on `overall` - the number the gauge actually SHOWED - because that is
    the whole product for someone who never sees the factors underneath it. That
    includes gated days: if the gauge reads 93 on a VETO morning, the user can act
    on 93, so it has to be scored as the GO call it looks like. (The old basis
    graded the raw pre-gate score and dropped VETO days entirely, which measured
    something nobody is ever shown.)

    `raw_score` reports the same win rates on `direction_quality` with the gate
    left out, so the two can be compared directly: if folding the gate into the
    needle earns its keep, the shown basis should beat the raw one.
    """
    cfg = get_config()
    good, caution = cfg["thresholds"]["good"], cfg["thresholds"]["caution"]
    rows = [r for r in recent_history(limit=10000) if r["realized_label"] is not None]

    summary = {
        "samples": len(rows), "good_threshold": good, "caution_threshold": caution,
        "basis": "overall",
        "avg_er_good": None, "avg_er_avoid": None,
        "veto_days": 0, "veto_chop_rate": None,
        "go_n": 0, "go_win_rate": None,
        "avoid_n": 0, "avoid_hit_rate": None,
        "overall_n": 0, "overall_win_rate": None,
        "raw_score": None,
    }
    if not rows:
        return summary

    shown = [(r["overall"], r) for r in rows
             if r["tier"] != "CLOSED" and r["overall"] is not None]
    raw = [(r["direction_quality"], r) for r in rows
           if r["tier"] not in ("VETO", "CLOSED") and r["direction_quality"] is not None]

    summary.update(_decisive_stats(shown, good, caution))
    summary["raw_score"] = _decisive_stats(raw, good, caution)

    veto = [r for r in rows if r["tier"] == "VETO"]
    summary["veto_days"] = len(veto)
    if veto:
        summary["veto_chop_rate"] = round(
            sum(1 for r in veto if r["realized_label"] == "CHOPPY") / len(veto), 3)
    return summary
