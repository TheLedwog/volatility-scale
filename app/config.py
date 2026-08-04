"""Runtime-editable configuration, stored in SQLite (one row per top-level key).

`DEFAULTS` is the source of truth for shape; the DB holds user overrides so the
Settings page can change weights/thresholds/lists without a redeploy.
"""
from __future__ import annotations

import json
import os

try:  # load a local .env (git-ignored) if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    pass

from .db import get_conn, init_db

DEFAULTS: dict = {
    "session": {"tz": "America/New_York", "open": "09:30", "close": "16:00"},
    "tickers": {
        "primary": "^GSPC",     # S&P 500 index
        "secondary": "^IXIC",   # NASDAQ Composite
        "futures": "ES=F",      # S&P 500 e-mini (overnight read)
        "vix": "^VIX",
        "vix3m": "^VIX3M",
    },
    # Each factor outputs a chop-risk in [0,1]; weights must sum to ~1.0.
    "weights": {
        "prior_day_efficiency": 0.30,
        "event_noise": 0.20,
        "vix_regime": 0.20,
        "overnight_range": 0.15,
        "structural_day": 0.15,
        "news_risk": 0.25,   # optional; only counts when news is GPT-scored
    },
    "thresholds": {
        "good": 65,                 # direction_quality >= good  -> "good to trade"
        "caution": 40,              # < caution -> "choppy, avoid"
        # The scale you SEE folds the gate into the raw score by DISCOUNTING it, so
        # the needle stays fluid and monotonic - a clean-setup veto day still reads
        # higher than an ugly one instead of snapping to a flat floor. A VETO
        # multiplies the score by veto_score_multiplier, a WARN by warn_score_multiplier
        # (CLEAN = x1.0). These are only the PRIOR: the calibration section below learns
        # the real multiplier from labelled outcomes and shrinks toward these when data
        # is thin (see app/scoring/calibration.py).
        "veto_score_multiplier": 0.25,
        "warn_score_multiplier": 0.6,
        "dead_day_range_pct": 0.40, # ATR% below this -> low-opportunity flag
        # Cutoffs that turn the realized full-session 5-min efficiency ratio into a
        # CHOPPY/MIXED/DIRECTIONAL label, set as PERCENTILES of the measured 5-min
        # distribution (59 sessions: p30 0.040, median 0.097, p70 0.147, p90 0.226,
        # p95 0.271). That window is representative - its hourly-ER median 0.389
        # matches the 3-year backfill's 0.390.
        #
        # These were 0.28/0.08, which is the top 5% and the bottom 42%: DIRECTIONAL
        # was so rare that "we said GO and the day trended" was near unwinnable by
        # construction - a perfect oracle scores ~5% - so the track record measured
        # the threshold, not the tool. At p70/p30 the mix is roughly 30/40/30 and a
        # win rate can actually be won or lost.
        #
        # Changing these makes stored labels incomparable, so app.labeling.efficiency
        # re-grades on startup when they move (yfinance only serves ~60 days of 5-min
        # bars, so a delayed re-grade strands older rows on the old basis forever).
        "label_directional_er": 0.15,
        "label_choppy_er": 0.04,
    },
    "gate": {
        # Categories that VETO the day if scheduled intra-session.
        "veto_categories": ["monetary_policy", "ism", "jolts", "consumer_confidence"],
        # Categories that only WARN (typically 8:30 ET pre-open giants).
        "warn_categories": ["cpi", "nfp", "ppi", "pce", "gdp", "retail_sales"],
        "min_impact": "High",       # only High-impact events trip the gate
        "session_buffer_min": 15,   # widen veto window this many minutes before open
    },
    # Soft-score engine: "auto" uses the trained model if data/model.joblib exists,
    # else falls back to the rule-based factors. "rules" / "model" force one.
    "scoring": {"mode": "auto"},
    # Learns the VETO/WARN discount from realized outcomes (mirrors CALIB_DEFAULTS in
    # app/scoring/calibration.py). Shrinks toward the thresholds priors when data is thin.
    "calibration": {
        "enabled": True,
        "pseudocount": 6,          # shrinkage strength: higher trusts the prior for longer
        "min_baseline_days": 5,    # labelled CLEAN days needed before trusting the learned discount
        "category_min_samples": 3, # show a per-category row on History once it has this many days
        "multiplier_floor": 0.05,  # noise guards (NOT a red-cap; pure-data can exceed the caution line)
        "multiplier_ceiling": 1.5,
    },
    "ml": {
        "lookback_days_daily": 800,
        "lookback_days_hourly": 730,   # yfinance hourly history cap
        "min_session_bars": 4,         # min hourly bars to label a session
        "model_file": "model.joblib",
        "test_fraction": 0.25,         # chronological hold-out for metrics
        # In "auto" mode the model is only used if it clears these on its hold-out,
        # i.e. it must actually beat near-random before replacing the rules.
        "min_spearman": 0.05,
        "min_lift": 0.02,
    },
    "schedule": {
        "enabled": True,
        # The frozen verdict is cut at the OPEN. It used to run at 08:45, which put it
        # 15 minutes after the 08:30 data drop - right in the middle of the reaction,
        # so the overnight/futures factors read a half-digested CPI/NFP spike. At 09:30
        # the full pre-market reaction is in the bars.
        "predict_time": "09:30",
        "label_time": "16:20",
        # Calendar refreshes run EVERY day, weekends included: FF rolls its one-week
        # feed over to the new week some time after Friday's close, and the weekend
        # runs are what pull next week's events in for the week-ahead view.
        "calendar_refresh_times": ["06:00", "12:00", "18:00", "22:00"],
        # Warm the news read before the open. GDELT rate-limits its free endpoint, and
        # one 429 landing on the predict job blanks a factor worth 25% of the weight
        # for the whole session - so try well ahead of it. Each run no-ops once the
        # day is cached and scored, so these cost one upstream call, not four.
        "news_refresh_times": ["07:30", "08:30", "09:20"],
    },
    "calendar": {
        # Hours before the cached calendar is considered stale. The economic calendar
        # barely moves intraday, and the feed is rate-limited, so refreshing a handful
        # of times a day is plenty.
        "max_age_hours": 12,
    },
    "providers": {
        "calendar": "forexfactory",
        "llm_provider": "openai",
        "openai_api_key": "",
        "openai_model": "gpt-5.6-luna",
        "news_api_key": "",
        "webz_api_key": "",
    },
    "news": {
        "enabled": True,            # fetch headlines; GPT read needs an OpenAI key
        # "webz" = Webz.io primary with GDELT as the automatic backstop (the chain is
        # built in app/providers/__init__.py). "gdelt" forces the old single feed.
        "provider": "webz",
        "max_headlines": 25,
        # Relevance sort + GDELT GKG *theme* filters (not loose keyword OR over full
        # article text, which pulled newest-anything junk like celebrity/gadget stories).
        "sort": "hybridrel",
        "require_finance_terms": True,  # drop any headline whose title isn't market-related
        "extra_finance_terms": [],      # optional extra keep-terms (e.g. specific tickers)
        "query": ('(theme:ECON_STOCKMARKET OR theme:ECON_INTEREST_RATE OR '
                  'theme:ECON_INFLATION OR theme:ECON_CENTRALBANK) sourcelang:eng'),
        # --- Webz.io ---------------------------------------------------------------
        # Elasticsearch boolean syntax. Filtering at SOURCE on title terms + finance
        # sites is the point: GDELT's theme query returned globally-themed noise (four
        # RBI rate stories, Turkish CPI, German retail sales) that the local relevance
        # net can only trim, never replace, so the GPT read was fed whatever survived.
        # Sorted by relevancy, NOT recency: sorting by crawl time surfaces the
        # algorithmic press-release mills (watchlistnews, financialcontent) that
        # publish constantly, while relevancy surfaces actual market coverage. The
        # exclusions drop evergreen retail listicles ("3 Stocks Worth Your
        # Attention", "Best high-yield savings rates") which pass a finance keyword
        # filter but say nothing about how today will trade.
        "webz_query": (
            'title:("stock market" OR "Wall Street" OR "Federal Reserve" OR FOMC OR '
            'Powell OR inflation OR CPI OR "interest rate" OR "rate cut" OR "rate hike" OR '
            'tariffs OR recession OR "S&P 500" OR Nasdaq OR Treasury OR yields OR '
            'payrolls OR jobs) '
            'language:english site_category:financial_news '
            '-title:("Worth Your Attention" OR "high-yield savings" OR "Should You Buy" OR '
            '"Best CD" OR "Prediction" OR "Reasons to" OR "Better Buy" OR "Options Volume" OR '
            '"Reaffirms" OR "Price Target" OR "Shares Sold" OR "Stake")'
        ),
        "webz_sort": "relevancy",
        # Free tier serves 10 results/call, so a headline set is paginated. This caps
        # the pages one fetch will pull - the quota is 500 calls/month and a trading
        # month is ~21 days, so 3 keeps a wide margin.
        "webz_max_calls": 3,
        "webz_page_size": 10,
        # Shared by both feeds: retry the transient statuses (429 especially) before
        # giving up on a factor worth 25% of the weight.
        "fetch_retries": 3,
        "fetch_retry_backoff_sec": 2.0,
    },
}

# Top-level keys exposed/edited as JSON blocks on the Settings page.
EDITABLE_SECTIONS = list(DEFAULTS.keys())


def _deepcopy(obj):
    return json.loads(json.dumps(obj))


def get_config() -> dict:
    """Return DEFAULTS overlaid with any DB overrides (per top-level key)."""
    init_db()
    cfg = _deepcopy(DEFAULTS)
    conn = get_conn()
    try:
        for row in conn.execute("SELECT key, value FROM config"):
            if row["key"] in cfg:
                try:
                    cfg[row["key"]] = json.loads(row["value"])
                except json.JSONDecodeError:
                    pass
    finally:
        conn.close()
    return cfg


def set_section(key: str, value) -> None:
    """Persist one top-level config section."""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO config(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        conn.commit()
    finally:
        conn.close()


def reset() -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM config")
        conn.commit()
    finally:
        conn.close()


def openai_api_key(cfg: dict | None = None) -> str:
    """Resolve the OpenAI key: OPENAI_API_KEY env var first, then stored config.

    The env var is preferred so the key need never be written to the DB or shown
    in the Settings UI.
    """
    cfg = cfg or get_config()
    return os.environ.get("OPENAI_API_KEY") or cfg.get("providers", {}).get("openai_api_key", "") or ""


def _webz_token(raw: str) -> str:
    """Accept a bare token OR the ready-made endpoint URL the Webz dashboard shows.

    The dashboard hands you a full filterWebContent URL with the token already in
    its query string, so that URL is what people actually have; asking them to
    dissect it by hand is just an opportunity to paste the wrong half. Any query
    baked into that URL is ignored - we build our own `q` from news.webz_query.
    """
    raw = (raw or "").strip().strip('"').strip("'")
    if "token=" not in raw:
        return raw
    from urllib.parse import parse_qs, urlparse

    return (parse_qs(urlparse(raw).query).get("token") or [""])[0].strip()


def webz_api_key(cfg: dict | None = None) -> str:
    """Resolve the Webz.io token: WEBZ_API_KEY env var first, then stored config."""
    cfg = cfg or get_config()
    raw = os.environ.get("WEBZ_API_KEY") or cfg.get("providers", {}).get("webz_api_key", "") or ""
    return _webz_token(raw)


def openai_key_status(cfg: dict | None = None) -> dict:
    """Where the active OpenAI key comes from, for the Settings UI.

    `source` is "environment" (the env var, which wins), "stored" (saved in the
    DB), or "none". `active` is True if any key is resolvable.
    """
    cfg = cfg or get_config()
    env_present = bool(os.environ.get("OPENAI_API_KEY"))
    stored_present = bool(cfg.get("providers", {}).get("openai_api_key"))
    source = "environment" if env_present else "stored" if stored_present else "none"
    return {
        "env_present": env_present,
        "stored_present": stored_present,
        "source": source,
        "active": env_present or stored_present,
    }
