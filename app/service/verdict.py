"""Render-time verdict helpers, extracted verbatim from the web routes.

These used to live as private functions in app/main.py. They were pulled out so
the JSON API (app/api) computes the gauge / state / news-blind flags with exactly
the same code that renders the Jinja dashboard - the whole point of the product
split is that the frontend can trust these numbers match the internal view.
"""
from __future__ import annotations

from datetime import datetime

from ..scoring.calibration import calibrate, day_category, resolve_multiplier
from ..timeutils import fmt_et


def computed_at_str(pred: dict | None) -> str | None:
    """Format a prediction's created_at as 'HH:MM ET' for the frozen-snapshot label."""
    if not pred or not pred.get("created_at"):
        return None
    try:
        return fmt_et(datetime.fromisoformat(pred["created_at"]))
    except (ValueError, TypeError):
        return None


def display_state(pred: dict, cfg: dict) -> str:
    tier = pred["tier"]
    if tier in ("VETO", "CLOSED"):
        return tier.lower()
    if tier == "WARN":
        return "warn"
    dq = pred.get("direction_quality") or 0
    if dq >= cfg["thresholds"]["good"]:
        return "good"
    if dq < cfg["thresholds"]["caution"]:
        return "avoid"
    return "mixed"


def tier_multiplier(pred: dict, cfg: dict, cal: dict | None = None) -> tuple[float, str | None]:
    """The learned discount applied to the gauge for this day, plus its category.

    Returns (1.0, None) for CLEAN/CLOSED days where the gate does not discount.

    The multiplier is FROZEN onto the prediction at predict time (engine.py) from
    the sessions graded before it. Reading it back - rather than recomputing from
    current calibration - is what stops a day's gauge from moving after the fact,
    and stops a graded day from feeding the multiplier applied to itself. Rows
    written before freezing existed fall back to a live computation.
    """
    tier = pred.get("tier")
    if tier not in ("VETO", "WARN"):
        return 1.0, None
    features = pred.get("features") or {}
    category = day_category(features, tier, cfg)
    stored = features.get("gate_multiplier")
    if isinstance(stored, (int, float)) and not isinstance(stored, bool):
        return float(stored), category
    cal = cal or calibrate(cfg)
    return resolve_multiplier(cal, cfg, tier, category), category


def overall_score(pred: dict, cfg: dict, cal: dict | None = None) -> int | None:
    """The score the gauge SHOWS: raw direction-quality with the gate folded in.

    The gate DISCOUNTS the score (multiplies it) rather than capping it, so the
    needle stays fluid and monotonic - a clean-setup veto day still reads higher
    than an ugly one. The multiplier is LEARNED per tier/event-category from
    realized outcomes (calibration.py), shrinking toward the config prior when
    data is thin. A clean day is the raw score unchanged.
    """
    dq = pred.get("direction_quality")
    if dq is None:
        return None
    tier = pred.get("tier")
    if tier not in ("VETO", "WARN"):
        return dq
    mult, _category = tier_multiplier(pred, cfg, cal)
    return max(0, min(100, int(round(dq * mult))))


def news_blind(pred: dict, cfg: dict) -> bool:
    """True when news is enabled but today has no GPT-scored read folded into the score."""
    if not cfg.get("news", {}).get("enabled", False):
        return False
    nw = (pred.get("features") or {}).get("news")
    return not (nw and nw.get("scored"))
