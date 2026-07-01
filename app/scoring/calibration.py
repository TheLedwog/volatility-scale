"""Learn the tier discount (VETO / WARN multiplier) from realized outcomes.

The gauge shows `direction_quality x tier_multiplier`. Instead of a fixed prior
(thresholds.veto_score_multiplier / warn_score_multiplier), this learns the
multiplier from how VETO / WARN days *actually* traded vs. normal (CLEAN) days:

    raw_m = mean(realized ER on those days) / mean(realized ER on CLEAN days)

"veto days trade at X% of a normal day". To stay reliable when data is thin it
uses HIERARCHICAL SHRINKAGE (empirical-Bayes with a pseudo-count k):

    pooled_m[tier]        = shrink(raw_pooled,   prior,          n_tier, k)
    category_m[tier][cat] = shrink(raw_category, pooled_m[tier], n_cat,  k)

so a category with few days is pulled toward the pooled tier estimate, and the
pooled estimate is pulled toward the fixed prior. No hard sample cutoffs; it
self-updates every time a session is labelled. Per the product decision the
result is NOT capped into the red - if the data says veto days trade well, the
needle is free to rise (only a wide numeric floor/ceiling guards against noise).
"""
from __future__ import annotations

import json

from ..config import get_config
from ..db import get_conn, init_db

# Defaults for the `calibration` config section (merged over any DB override so a
# partial override can't drop a key).
CALIB_DEFAULTS = {
    "enabled": True,
    "pseudocount": 6,          # shrinkage strength: higher trusts the prior for longer
    "min_baseline_days": 5,    # need this many labelled CLEAN days before trusting the learned discount
    "category_min_samples": 3, # show a per-category row on History once it has this many days
    "multiplier_floor": 0.05,  # noise guards (NOT a red-cap; pure-data can exceed the caution line)
    "multiplier_ceiling": 1.5,
}

_PRIOR_KEY = {"VETO": "veto_score_multiplier", "WARN": "warn_score_multiplier"}
_PRIOR_DEFAULT = {"VETO": 0.25, "WARN": 0.6}
_IMPACT_RANK = {"high": 3, "medium": 2, "low": 1, "holiday": 0}


def _rank(impact: str) -> int:
    return _IMPACT_RANK.get((impact or "").strip().lower(), 0)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _shrink(raw, target: float, n: int, k: float) -> float:
    """Weighted blend of the data estimate and a fallback target by sample count."""
    if raw is None or n <= 0:
        return target
    return (n * raw + k * target) / (n + k)


def _prior(cfg: dict, tier: str) -> float:
    return float(cfg["thresholds"].get(_PRIOR_KEY[tier], _PRIOR_DEFAULT[tier]))


def day_category(features: dict, tier: str, cfg: dict) -> str | None:
    """The event category that put a day into its tier (for per-category learning).

    Prefers the explicit `gate_primary_category` stored at predict time; falls back
    to reconstructing it from the stored enriched events for older rows.
    """
    if not features:
        return None
    explicit = features.get("gate_primary_category")
    if explicit:
        return explicit
    gate = cfg.get("gate", {})
    min_rank = _IMPACT_RANK.get(str(gate.get("min_impact", "High")).lower(), 3)
    veto_cats = set(gate.get("veto_categories", []))
    warn_cats = set(gate.get("warn_categories", []))
    for e in features.get("events") or []:
        cat = e.get("category")
        if not cat or _rank(e.get("impact")) < min_rank:
            continue
        if tier == "VETO" and cat in veto_cats and e.get("intra_session"):
            return cat
        if tier == "WARN" and (cat in warn_cats
                               or (cat in veto_cats and not e.get("intra_session"))):
            return cat
    return None


def _labeled_rows() -> list[tuple]:
    """(tier, features, realized_er, realized_label) for every labelled session."""
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT p.tier AS tier, p.features_json AS fj,
                   o.realized_er AS er, o.realized_label AS lab
            FROM predictions p JOIN outcomes o ON o.date = p.date
            WHERE o.realized_er IS NOT NULL
            """
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            f = json.loads(r["fj"] or "{}")
        except (json.JSONDecodeError, TypeError):
            f = {}
        out.append((r["tier"], f, r["er"], r["lab"]))
    return out


def calibrate(cfg: dict | None = None) -> dict:
    """Compute the learned tier/category multipliers + supporting stats for the UI."""
    cfg = cfg or get_config()
    cc = {**CALIB_DEFAULTS, **(cfg.get("calibration") or {})}
    k = float(cc["pseudocount"])
    floor, ceil = float(cc["multiplier_floor"]), float(cc["multiplier_ceiling"])
    min_base, cat_min = int(cc["min_baseline_days"]), int(cc["category_min_samples"])

    rows = _labeled_rows()
    clean = [er for (t, _f, er, _l) in rows if t == "CLEAN" and er is not None]
    baseline = (sum(clean) / len(clean)) if clean else None
    ready = bool(cc["enabled"] and baseline and baseline > 0.01 and len(clean) >= min_base)

    out = {
        "enabled": bool(cc["enabled"]), "ready": ready,
        "baseline_er": round(baseline, 3) if baseline else None,
        "baseline_n": len(clean), "pseudocount": k,
        "min_baseline_days": min_base, "category_min_samples": cat_min, "tiers": {},
    }

    for tier in ("VETO", "WARN"):
        trows = [(f, er, lab) for (t, f, er, lab) in rows if t == tier and er is not None]
        ers = [er for (_f, er, _l) in trows]
        n = len(ers)
        prior = _prior(cfg, tier)
        pooled_raw = (sum(ers) / n / baseline) if (ready and n) else None
        pooled_m = _clamp(_shrink(pooled_raw, prior, n if ready else 0, k), floor, ceil)

        by_cat: dict = {}
        for (f, er, lab) in trows:
            by_cat.setdefault(day_category(f, tier, cfg), []).append((er, lab))
        cats = {}
        for c, items in by_cat.items():
            if c is None:
                continue
            cers = [e for (e, _l) in items]
            cn = len(cers)
            craw = (sum(cers) / cn / baseline) if (ready and cn) else None
            cm = _clamp(_shrink(craw, pooled_m, cn if ready else 0, k), floor, ceil)
            cats[c] = {
                "m": round(cm, 3), "raw": round(craw, 3) if craw is not None else None,
                "n": cn, "mean_er": round(sum(cers) / cn, 3),
                "chop_rate": round(sum(1 for (_e, lab) in items if lab == "CHOPPY") / cn, 3),
            }

        out["tiers"][tier] = {
            "prior": prior, "pooled_m": round(pooled_m, 3),
            "pooled_raw": round(pooled_raw, 3) if pooled_raw is not None else None,
            "n": n, "mean_er": round(sum(ers) / n, 3) if n else None,
            "chop_rate": round(sum(1 for (_f, _e, lab) in trows if lab == "CHOPPY") / n, 3) if n else None,
            "categories": cats,
        }
    return out


def resolve_multiplier(cal: dict, cfg: dict, tier: str, category: str | None) -> float:
    """The multiplier to apply for a given day: category -> pooled tier -> prior."""
    if tier not in ("VETO", "WARN"):
        return 1.0
    if not cal.get("ready"):
        return _prior(cfg, tier)
    t = cal["tiers"][tier]
    if category and category in t["categories"]:
        return t["categories"][category]["m"]
    return t["pooled_m"]
