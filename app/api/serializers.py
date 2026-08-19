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
    # Which cut applies depends on which engine produced the stored score: the
    # tradeability score is a PERCENTILE, so the rule engine's 0-100 chop thresholds
    # do not describe it. `scored_by` is written at predict time, so a row keeps being
    # read against the cut it was actually made under even after the mode changes.
    cfg_t = cfg.get("tradeability", {})
    caution_cut = (int(cfg_t.get("band_caution", 38))
                   if features.get("scored_by") == "tradeability" else th["caution"])

    return {
        "has_prediction": True,
        "date": pred.get("date"),
        "computed_at": computed_at_str(pred),
        "tier": tier,
        "verdict": pred.get("verdict"),
        "state": display_state(pred, cfg),
        "trade_ok": tier not in ("VETO", "CLOSED") and (dq is None or dq >= caution_cut),
        "reason": pred.get("reason"),
        "warn_note": pred.get("warn_note"),
        "dead_day": bool(features.get("dead_day")),
        "calendar_unavailable": bool(features.get("calendar_unavailable")),
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
        "tradeability": _tradeability_block(cfg, features),
        # The legs travel inside the stored tradeability blob (that is what predict
        # persists), not as a sibling key - read them from there.
        "legs": (features.get("tradeability") or {}).get("legs") or [],
    }


def _tradeability_block(cfg: dict, features: dict) -> dict:
    """The tradeability read for one stored prediction.

    Always returned (never None) so a client can rely on the key existing and read
    `mode` to know whether the engine is shadowing or driving the headline.
    """
    cfg_t = cfg.get("tradeability", {})
    t = features.get("tradeability") or {}
    return {
        "mode": cfg_t.get("mode", "shadow") if cfg_t.get("enabled", False) else "off",
        "score": t.get("score"),
        "band": t.get("band"),
        "expected_net_pct": t.get("expected_net_pct"),
        "expected_range_pct": t.get("expected_range_pct"),
        "expected_efficiency": t.get("expected_efficiency"),
        "fitted_at": t.get("fitted_at"),
        "n_samples": t.get("n_samples"),
        "error": t.get("error"),
    }
