"""The prediction engine: combine the gate + factors into a stored verdict."""
from __future__ import annotations

import json
from datetime import date

from ..config import get_config
from ..db import get_conn
from ..market_calendar import is_trading_day
from ..providers import get_calendar_provider, get_price_provider
from ..timeutils import now_et, today_et
from .factors import build_context, compute_factors
from .gate import decide_gate


def _verdict(tier: str, dq: int, cfg: dict, dead_day: bool) -> tuple[str, str, bool]:
    th = cfg["thresholds"]
    if tier == "CLOSED":
        return "Market closed", "No NY session today.", False
    if tier == "VETO":
        return "DON'T TRADE", "High-impact event scheduled during the session.", False

    if dq >= th["good"]:
        label, msg, ok = "Good to trade", "Conditions look directional.", True
    elif dq < th["caution"]:
        label, msg, ok = "Choppy - avoid", "Conditions look choppy / low-direction.", False
    else:
        label, msg, ok = "Mixed - be selective", "Mixed conditions; pick spots carefully.", True

    if dead_day:
        msg += " Low expected range (possible dead day)."
    if tier == "WARN":
        label = "Caution: " + label
        msg = "Big pre-open data today. " + msg
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
    calendar = get_calendar_provider()

    calendar_error = None
    try:
        events = calendar.events_for(d)
    except Exception as exc:  # noqa: BLE001 - degrade if the feed is down
        events, calendar_error = [], str(exc)

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

    tier = gate["tier"]
    dq = f["direction_quality"]
    label, msg, ok = _verdict(tier, dq, cfg, f["dead_day"])

    features = {
        "factors": f["factors"],
        "breakdown_kind": f.get("breakdown_kind", "rules"),
        "predicted_er": f.get("predicted_er"),
        "model_version": f.get("model_version"),
        "model_note": model_note,
        "gate_tier": tier,
        "dead_day": f["dead_day"],
        "atr_pct": f["atr_pct"],
        "events": _serializable_events(gate["events"]),
        "calendar_error": calendar_error,
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
        "events": gate["events"],
        "dead_day": f["dead_day"],
        "trade_ok": ok,
        "message": msg,
        "calendar_error": calendar_error,
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
