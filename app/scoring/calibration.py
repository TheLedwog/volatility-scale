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
    "category_min_samples": 3, # days a category needs before its OWN multiplier is used
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


def _labeled_rows(as_of: str | None = None) -> list[tuple]:
    """(tier, features, realized_er, realized_label) for every labelled session.

    `as_of` (YYYY-MM-DD) restricts the view to sessions graded strictly BEFORE that
    date - what the tool could actually have known on the morning of `as_of`.
    """
    init_db()
    conn = get_conn()
    sql = """
        SELECT p.tier AS tier, p.features_json AS fj,
               o.realized_er AS er, o.realized_label AS lab
        FROM predictions p JOIN outcomes o ON o.date = p.date
        WHERE o.realized_er IS NOT NULL
    """
    params: tuple = ()
    if as_of:
        sql += " AND p.date < ?"
        params = (as_of,)
    try:
        rows = conn.execute(sql, params).fetchall()
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


def calibrate(cfg: dict | None = None, as_of: str | None = None) -> dict:
    """Compute the learned tier/category multipliers + supporting stats for the UI.

    Pass `as_of` (a session date) to learn only from sessions graded before it. The
    gauge freezes its multiplier this way, so a day's score can never be derived
    from that day's own outcome.
    """
    cfg = cfg or get_config()
    cc = {**CALIB_DEFAULTS, **(cfg.get("calibration") or {})}
    k = float(cc["pseudocount"])
    floor, ceil = float(cc["multiplier_floor"]), float(cc["multiplier_ceiling"])
    min_base, cat_min = int(cc["min_baseline_days"]), int(cc["category_min_samples"])

    rows = _labeled_rows(as_of)
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


def frozen_multiplier(cfg: dict, tier: str, category: str | None, date_str: str) -> float:
    """The multiplier to STORE on a prediction: resolved from prior sessions only."""
    if tier not in ("VETO", "WARN"):  # no gate, no calibration read
        return 1.0
    return resolve_multiplier(calibrate(cfg, as_of=date_str), cfg, tier, category)


def backfill_gate_multipliers(cfg: dict | None = None) -> int:
    """Stamp the as-of multiplier onto stored predictions written before it was frozen.

    Those rows carry no `gate_multiplier`, so the gauge re-derived one from TODAY's
    calibration every time they were rendered: a day's displayed score drifted after
    the fact, and once the day itself was graded its own outcome fed the multiplier
    applied to it (one ISM day scoring 62 x 1.5 = 93 under a DON'T TRADE verdict).
    Replay each row against only the sessions graded before it and store the result.

    Idempotent - rows already carrying a multiplier are skipped, so this is safe to
    run on every startup. Returns the number of rows stamped.
    """
    cfg = cfg or get_config()
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT date, tier, features_json FROM predictions ORDER BY date"
        ).fetchall()
        stamped = 0
        for r in rows:
            try:
                features = json.loads(r["features_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(features, dict) or "gate_multiplier" in features:
                continue
            tier = r["tier"]
            category = day_category(features, tier, cfg) if tier in ("VETO", "WARN") else None
            features["gate_multiplier"] = round(
                frozen_multiplier(cfg, tier, category, r["date"]), 4)
            conn.execute(
                "UPDATE predictions SET features_json=? WHERE date=?",
                (json.dumps(features, default=str), r["date"]),
            )
            stamped += 1
        conn.commit()
        return stamped
    finally:
        conn.close()


def resolve_multiplier(cal: dict, cfg: dict, tier: str, category: str | None) -> float:
    """The multiplier to apply for a given day: category -> pooled tier -> prior.

    A category has to EARN its own multiplier: until it has `category_min_samples`
    graded days it scores off the pooled tier estimate instead. One ISM day that
    happened to trend hard otherwise sets that category's multiplier by itself -
    a single sample produced a raw 5.19x, which the ceiling then clamped to 1.5,
    so the gauge was showing the clamp constant rather than anything learned.
    Shrinkage alone doesn't save you here: it pulls a thin estimate toward the
    pooled value but still lets one outlier drag it a long way.
    """
    if tier not in ("VETO", "WARN"):
        return 1.0
    if not cal.get("ready"):
        return _prior(cfg, tier)
    t = cal["tiers"][tier]
    cat = t["categories"].get(category) if category else None
    if cat and cat["n"] >= int(cal.get("category_min_samples", 3)):
        return cat["m"]
    return t["pooled_m"]
