"""Turn stored engine output into the API DTOs (dicts matching app.api.schemas)."""
from __future__ import annotations

from ..providers.base import filter_market_headlines
from ..scoring.calibration import calibrate
from ..service.verdict import (
    computed_at_str,
    display_state,
    news_blind,
    overall_score,
    tier_multiplier,
)


def _news_block(pred: dict, cfg: dict, features: dict) -> dict:
    ncfg = cfg.get("news", {})
    if not ncfg.get("enabled", False):
        return {"enabled": False, "scored": False, "blind": False, "headlines": []}
    nw = features.get("news") or {}
    headlines = nw.get("headlines") or []
    if ncfg.get("require_finance_terms", True):
        # Same market-relevance scrub the dashboard applies, so old cached junk
        # headlines are hidden in the API too.
        headlines = filter_market_headlines(headlines, ncfg.get("extra_finance_terms"))
    return {
        "enabled": True,
        "scored": bool(nw.get("scored")),
        "blind": news_blind(pred, cfg),
        "direction": nw.get("direction"),
        "expected_impact": nw.get("expected_impact"),
        "chop_risk": nw.get("chop_risk"),
        "rationale": nw.get("rationale"),
        "headlines": headlines,
        "source": nw.get("source"),
        "error": nw.get("error"),
    }


def serialize_today(pred: dict | None, cfg: dict, cal: dict | None = None) -> dict:
    if not pred:
        return {"has_prediction": False, "factors": [], "events": []}

    cal = cal or calibrate(cfg)
    features = pred.get("features") or {}
    tier = pred.get("tier")
    dq = pred.get("direction_quality")
    mult, _category = tier_multiplier(pred, cfg, cal)
    th = cfg["thresholds"]

    return {
        "has_prediction": True,
        "date": pred.get("date"),
        "computed_at": computed_at_str(pred),
        "tier": tier,
        "verdict": pred.get("verdict"),
        "state": display_state(pred, cfg),
        "trade_ok": tier not in ("VETO", "CLOSED") and (dq is None or dq >= th["caution"]),
        "reason": pred.get("reason"),
        "warn_note": pred.get("warn_note"),
        "dead_day": bool(features.get("dead_day")),
        "gauge": {
            "overall": overall_score(pred, cfg, cal),
            "direction_quality": dq,
            "multiplier": round(mult, 3),
            "tier_discounted": tier in ("VETO", "WARN"),
        },
        "news": _news_block(pred, cfg, features),
        "model": {
            "mode": cfg.get("scoring", {}).get("mode", "auto"),
            "kind": features.get("breakdown_kind", "rules"),
            "note": features.get("model_note"),
        },
        "factors": features.get("factors") or [],
        "events": features.get("events") or [],
    }
