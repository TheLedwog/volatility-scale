"""The prediction engine: combine the gate + factors into a stored verdict."""
from __future__ import annotations

import json
from datetime import date

from ..config import get_config
from ..db import get_conn
from ..jobs.calendar_refresh import ensure_calendar
from ..market_calendar import is_trading_day
from ..providers import get_price_provider
from ..service.calendar_view import gate_events
from ..timeutils import now_et, today_et
from .calibration import frozen_multiplier
from .factors import build_context, compute_factors
from .gate import decide_gate
from .tradeability import ensure_model, score_day


def _verdict(tier: str, dq: int, cfg: dict, dead_day: bool,
             calendar_unavailable: bool = False,
             cuts: tuple[int, int] | None = None) -> tuple[str, str, bool]:
    """Headline + message + trade_ok for a score.

    `cuts` is (good, caution). It defaults to the rule-engine thresholds; the
    tradeability engine passes its own percentile band cuts instead, because its
    score is a percentile and the two scales are not comparable. The verdict STRINGS
    are deliberately identical either way - the frontend switches on them.
    """
    th = cfg["thresholds"]
    good, caution = cuts if cuts else (th["good"], th["caution"])
    if tier == "CLOSED":
        return "Market closed", "No NY session today.", False
    if tier == "VETO":
        return "DON'T TRADE", "High-impact event scheduled during the session.", False

    if dq >= good:
        label, msg, ok = "Good to trade", "Conditions look directional.", True
    elif dq < caution:
        label, msg, ok = "Choppy - avoid", "Conditions look choppy / low-direction.", False
    else:
        label, msg, ok = "Mixed - be selective", "Mixed conditions; pick spots carefully.", True

    if dead_day:
        msg += " Low expected range (possible dead day)."
    if tier == "WARN":
        label = "Caution: " + label
        msg = "Big pre-open data today. " + msg

    # We have no calendar for this day, so the gate never really ran: CLEAN here means
    # "nothing known", not "nothing scheduled". Refuse to bless the day - an unseen
    # FOMC would otherwise read as "good to trade".
    if calendar_unavailable and tier != "VETO":
        label = "Calendar unavailable - treat as caution"
        msg = ("The economic calendar could not be loaded, so scheduled events could NOT "
               "be checked. " + msg)
        ok = False
    return label, msg, ok


def _serializable_events(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        e2 = {k: v for k, v in e.items() if k != "time"}
        out.append(e2)
    return out


def run_prediction(d: date | None = None) -> dict:
    cfg = get_config()
    d = d or today_et()
    created_at = now_et().isoformat(timespec="seconds")

    # Market closed -> short-circuit
    if not is_trading_day(d):
        result = {
            "date": d.isoformat(), "tier": "CLOSED", "direction_quality": None,
            "chop_risk": None, "verdict": "Market closed",
            "reason": "Market is closed today.", "warn_note": "",
            "factors": [], "events": [], "dead_day": False, "trade_ok": False,
            "message": "No NY session today.",
        }
        _store(result, created_at, features={"closed": True})
        return result

    price = get_price_provider()

    # The calendar comes from the CACHE, never a live fetch (app/calendar_store.py).
    # ForexFactory rate-limits, and a failed fetch used to degrade to an empty event
    # list - which the gate cannot tell apart from "nothing scheduled", so a 429 on an
    # FOMC morning graded the day CLEAN. Now: top the cache up if it's stale, then read
    # it. A fetch failure leaves the last-known calendar in place and the VETO stands.
    refresh = ensure_calendar(cfg, d)
    events, calendar_unavailable = gate_events(cfg, d)
    calendar_error = None if refresh.get("ok") else refresh.get("error")

    gate = decide_gate(events, cfg, d)

    # News / geopolitics (GDELT headlines + GPT read), if enabled. One call/day, cached.
    news_assessment = None
    if cfg.get("news", {}).get("enabled", False):
        try:
            from .news import get_news_assessment

            news_assessment = get_news_assessment(cfg, d)
        except Exception as exc:  # noqa: BLE001
            news_assessment = {"error": str(exc), "scored": False, "headlines": []}

    # Soft score: trained model when available/selected, else rule-based factors.
    mode = cfg.get("scoring", {}).get("mode", "auto")
    f, model_note = None, None
    if mode in ("auto", "model"):
        try:
            from ..ml.model import ModelScorer

            scorer = ModelScorer(cfg)
            if scorer.available() and (mode == "model" or scorer.is_useful()):
                f = scorer.score(cfg, price, d, news=news_assessment)
                if f is None:
                    model_note = "model present but insufficient live data; used rules"
            elif scorer.available():
                model_note = "model trained but not yet beating baseline; using rules"
            elif mode == "model":
                model_note = "model mode set but no model file; used rules"
        except Exception as exc:  # noqa: BLE001 - any ML failure -> rules
            model_note = f"model error ({exc}); used rules"
            f = None
    if f is None:
        ctx = build_context(cfg, price, events, d, news=news_assessment)
        f = compute_factors(cfg, ctx)

    # Tradeability engine. In "shadow" it is computed and stored but changes nothing
    # a user sees, so the two scores can be compared on real sessions before the
    # headline number moves. In "live" it BECOMES direction_quality.
    trade = None
    cfg_t = cfg.get("tradeability", {})
    if cfg_t.get("enabled", False):
        try:
            ensure_model(cfg, price)
            trade = score_day(cfg, price, d)
        except Exception as exc:  # noqa: BLE001 - never let the new engine break predict
            trade = {"error": str(exc)}

    tier = gate["tier"]
    dq = f["direction_quality"]
    cuts = None
    scored_by = "rules"
    if cfg_t.get("mode") == "live" and trade and trade.get("score") is not None:
        dq = trade["score"]
        cuts = (int(cfg_t.get("band_good", 60)), int(cfg_t.get("band_caution", 38)))
        scored_by = "tradeability"

    label, msg, ok = _verdict(tier, dq, cfg, f["dead_day"], calendar_unavailable, cuts)

    # The event category that set the tier - drives per-category discount learning.
    tier_events = gate.get("veto_events") if tier == "VETO" else gate.get("warn_events")
    primary_category = tier_events[0]["category"] if tier_events else None

    # Freeze the gate discount the gauge will show, learned from the sessions graded
    # BEFORE today. Calibration keeps updating as later days are labelled, so deriving
    # this at render time made a day's score drift after the fact - and once the day
    # was graded, its own outcome fed the multiplier applied to it.
    gate_multiplier = frozen_multiplier(cfg, tier, primary_category, d.isoformat())

    features = {
        "factors": f["factors"],
        "breakdown_kind": f.get("breakdown_kind", "rules"),
        # Always stored, in both modes: in shadow this is the comparison record, and
        # once live it is the audit trail for the number that was shown.
        "tradeability": trade,
        "scored_by": scored_by,
        "rules_direction_quality": f["direction_quality"],
        "predicted_er": f.get("predicted_er"),
        "model_version": f.get("model_version"),
        "model_note": model_note,
        "gate_tier": tier,
        "gate_primary_category": primary_category,
        "gate_multiplier": round(gate_multiplier, 4),
        "dead_day": f["dead_day"],
        "atr_pct": f["atr_pct"],
        "events": _serializable_events(gate["events"]),
        "calendar_error": calendar_error,
        "calendar_unavailable": calendar_unavailable,
        "news": news_assessment,
    }

    result = {
        "date": d.isoformat(),
        "tier": tier,
        "direction_quality": dq,
        "chop_risk": f["chop_risk"],
        "verdict": label,
        "reason": gate["reason"],
        "warn_note": gate["warn_note"],
        "factors": f["factors"],
        # ADDITIVE. `factors` keeps the rule-engine rows in BOTH modes so an existing
        # frontend never sees its shape change; the tradeability legs arrive beside it
        # under a new key the UI can adopt whenever it's ready. (They are persisted
        # inside features["tradeability"]["legs"]; this is the live return value.)
        "legs": (trade or {}).get("legs", []),
        "events": gate["events"],
        "dead_day": f["dead_day"],
        "trade_ok": ok,
        "message": msg,
        "calendar_error": calendar_error,
        "calendar_unavailable": calendar_unavailable,
    }
    _store(result, created_at, features)
    return result


def _store(result: dict, created_at: str, features: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO predictions
                (date, created_at, tier, direction_quality, chop_risk,
                 verdict, reason, warn_note, features_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                created_at=excluded.created_at, tier=excluded.tier,
                direction_quality=excluded.direction_quality,
                chop_risk=excluded.chop_risk, verdict=excluded.verdict,
                reason=excluded.reason, warn_note=excluded.warn_note,
                features_json=excluded.features_json
            """,
            (
                result["date"], created_at, result["tier"],
                result["direction_quality"], result["chop_risk"],
                result["verdict"], result["reason"], result["warn_note"],
                json.dumps(features, default=str),
            ),
        )
        conn.commit()
    finally:
        conn.close()
