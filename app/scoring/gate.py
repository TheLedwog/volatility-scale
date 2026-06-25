"""The hard gate: turn the day's calendar into a VETO / WARN / CLEAN tier.

- VETO: a high-impact event in a veto category is scheduled *during* the session.
- WARN: a high-impact pre-open giant (or a high-impact veto-category event that
  falls outside the session window) — caution, but the user's call.
- CLEAN: nothing on the calendar trips the gate.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..timeutils import fmt_et, session_window
from .categories import CATEGORY_LABELS, categorize

_IMPACT_RANK = {"high": 3, "medium": 2, "low": 1, "holiday": 0}


def _rank(impact: str) -> int:
    return _IMPACT_RANK.get((impact or "").strip().lower(), 0)


def enrich_events(events: list[dict], cfg: dict, d: date) -> list[dict]:
    """Attach category / intra-session flag to each event, sorted by time."""
    sess = cfg["session"]
    open_dt, close_dt = session_window(d, sess["open"], sess["close"])
    buffer = timedelta(minutes=cfg["gate"].get("session_buffer_min", 0))
    veto_start = open_dt - buffer

    enriched = []
    for e in events:
        cat = categorize(e.get("title", ""))
        t = e.get("time")
        intra = bool(t and veto_start <= t <= close_dt)
        enriched.append({
            "title": e.get("title", ""),
            "impact": e.get("impact", ""),
            "time": t,
            "time_str": fmt_et(t) if t else "All day / tentative",
            "category": cat,
            "category_label": CATEGORY_LABELS.get(cat, cat or "—"),
            "intra_session": intra,
        })
    enriched.sort(key=lambda x: (x["time"] is None, x["time"] or open_dt))
    return enriched


def decide_gate(events: list[dict], cfg: dict, d: date) -> dict:
    gate_cfg = cfg["gate"]
    min_rank = _IMPACT_RANK.get(str(gate_cfg.get("min_impact", "High")).lower(), 3)
    veto_cats = set(gate_cfg.get("veto_categories", []))
    warn_cats = set(gate_cfg.get("warn_categories", []))

    enriched = enrich_events(events, cfg, d)
    veto_events, warn_events = [], []

    for e in enriched:
        if e["category"] is None or _rank(e["impact"]) < min_rank:
            continue
        cat = e["category"]
        if cat in veto_cats and e["intra_session"]:
            veto_events.append(e)
        elif cat in warn_cats:
            warn_events.append(e)
        elif cat in veto_cats and not e["intra_session"]:
            warn_events.append(e)

    if veto_events:
        tier = "VETO"
    elif warn_events:
        tier = "WARN"
    else:
        tier = "CLEAN"

    reason = "; ".join(
        f"{e['category_label']} ({e['title']}) at {e['time_str']}" for e in veto_events
    )
    warn_note = "; ".join(
        f"{e['category_label']} at {e['time_str']}" for e in warn_events
    )

    return {
        "tier": tier,
        "reason": reason,
        "warn_note": warn_note,
        "veto_events": veto_events,
        "warn_events": warn_events,
        "events": enriched,
    }
