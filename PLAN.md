# Trade / Don't-Trade Scale — Design Plan

> A local tool that, each morning before the New York open, tells you whether the
> NY session of the NASDAQ (NQ) and S&P 500 (ES/SPX) is likely to **trend cleanly
> (trade)** or **chop with no direction (don't trade)** — shown as an arrow on a
> bar — and that **learns and self-corrects** by checking what actually happened
> after the close.

---

## 1. Core concept (the important reframe)

This is **not** a volatility scale. The strategy needs *big, directional* moves and
fails in *up-and-down, no-direction* conditions. A day can be very volatile and
great (one clean trend) or very volatile and terrible (whipsaw chop). Raw
volatility can't tell those apart — **directionality** can.

- **Output axis:**
  `CHOPPY / NO DIRECTION (don't trade)  ●───▷───  BIG DIRECTIONAL MOVES (trade)`
- **Primary goal (per user):** *avoid chop.* The score is essentially the
  predicted "directional quality" of today's NY session; low = stay out.

### Output tiers: VETO → WARN → score
The verdict is a three-state machine (highest tier wins):

1. **VETO — hard no-trade (overrides everything).** A genuinely market-moving event
   scheduled *during* the NY session locks the day to **DON'T TRADE** with the
   reason shown, regardless of the soft score. These produce big, wicky whipsaw
   candles that ruin entries. Default veto list:
   - **Monetary policy:** FOMC rate decision/statement, Powell presser, FOMC
     minutes, emergency rate moves (~14:00 ET).
   - **Top 10:00 ET data:** ISM, JOLTS, consumer confidence/sentiment.
   - Fires only for **high-impact** events ("news that actually moves the market"),
     not every red folder. Impact starts from this curated list and is later refined
     from each event type's *historical* NQ/SPX reaction (~30 min wick after it).
   - Veto list, impact threshold, and time window are all editable in settings.
   - **Vetoed days are still logged and labeled** — to validate the veto over time
     and flag any event type that actually trends cleanly.
2. **WARN — caution, your call.** The 8:30 ET pre-open giants (CPI, NFP, PPI, PCE,
   GDP, retail sales) land before the open, so they don't auto-veto, but the day
   gets a prominent caution flag. The chop score is still shown; the final decision
   is the user's.
3. **Score — the arrow bar.** On clean (and WARN) days, the learned model ranks the
   day on the chop-vs-direction axis below.

### The label the tool learns against
Computed **after** the close on the 9:30–16:00 ET session:

- **Kaufman Efficiency Ratio (ER)** = |session_close − session_open| ÷ Σ|bar-to-bar move|
  - ER ≈ 1 → straight-line trend → **good day**
  - ER ≈ 0 → lots of motion, no progress → **chop → bad day**
- **ADX** as a cross-check (trend strength).
- **Range/ATR** as a *minor* secondary input + a "dead day" guard (a flat,
  low-range day should not read as "trade" even if technically directional).

Thresholds are user-tunable. Long term, the best label is the user's own trade
P&L, if/when that can be fed in.

### How the ForexFactory rule changes
Today: "any red folder ⇒ don't trade." Two upgrades:
- **The big, intra-session market-movers keep their veto** (FOMC/rates etc.) — see
  the hard gate above — but it's now timing-aware (only events *during* the session)
  and impact-aware (only events that actually move price).
- **All other news becomes a feature, not the verdict** — the soft model learns
  *which* lesser event types (for NQ/SPX, NY session) tend to trend vs. whip,
  instead of blanket-avoiding every red folder.

---

## 2. Factors (features)

**Tier 1 — scheduled & reliable (build first):**
- Economic calendar (feeds **both** the hard gate and the soft score): CPI, PPI,
  NFP/jobs, FOMC decision + Powell presser, PCE, GDP, ISM, retail sales, JOLTS,
  Fed speakers. Per event: scheduled time vs. the 9:30–16:00 ET window (is it
  intra-session?), and an **impact weight** (curated list first, then learned from
  historical NQ/SPX reaction). Hard gate fires for high-impact intra-session events;
  everything else becomes soft-score features (counts, time-to-event, etc.).
- VIX level + 1-day change; VIX term structure (VIX vs VIX3M).
- Recent realized behavior: ATR, prior-day range, prior-day Efficiency Ratio,
  overnight ES/NQ futures gap & range.
- Structural days: OPEX / quad-witching, month/quarter-end, day before/after
  holidays, half-days.
- Mega-cap earnings (AAPL, MSFT, NVDA, AMZN, GOOGL, META) — big for NASDAQ.

**Tier 2 — unstructured news (wars, Trump/tariffs, geopolitics):**
- GDELT (free, global event + tone DB — strong for geopolitics) + financial RSS,
  paid news feed pluggable later.
- LLM (OpenAI/GPT) reads the day's headlines → structured JSON: market-relevance
  (0–1), risk-on/off/uncertain, expected impact, one-line rationale → feeds score.

---

## 3. The learning loop

1. **Pre-open job (~1 hr before 9:30 ET):** build features → score → store the
   prediction + every feature in the DB.
2. **Post-close job (after 16:00 ET):** compute the realized label (ER/ADX/range)
   → store outcome.
3. **Compare** prediction vs. reality → log error → update accuracy dashboard.
4. **Retrain / recalibrate** on accumulated history.

**Bootstrapping (so it's useful on day one, not in years):** there are only ~252
trading days/year, so we backfill ~2 years of free **hourly** data to train an
initial model now, and record proper **5-min** bars daily so the dataset compounds.
Deeper/cleaner intraday history can be purchased later if desired.

**Model progression:**
- Phase 1: transparent weighted-rule score (formalized FF rule + VIX + ATR + ER).
- Phase 2: LightGBM / logistic regression on the backfill → calibrated P(chop),
  with readable feature importances.
- Phase 3: continuous recalibration + LLM news layer.
- Avoid deep learning (data-hungry, opaque, overkill for a Pi).

**Validation:** walk-forward only — never shuffle time-series data (silent lookahead
bias). Calibrate probabilities (reliability curve) so "70% chop" means 70%.

---

## 4. Architecture (built to "easily change things later")

**One Python codebase. Low maintenance. Runs on a Raspberry Pi / Linux server.**

- **Backend — FastAPI**
  - Provider interfaces (swap free ⇄ paid by adding a class + an API key in settings,
    no code surgery): `PriceProvider`, `CalendarProvider`, `NewsProvider`,
    `LLMProvider`.
  - Factors as plug-in modules, each with a weight — add/remove/re-weight via config.
  - Config layer in the DB → weights, thresholds, schedule times, API keys editable
    at runtime from the settings UI (no redeploy).
  - Two scheduled jobs (cron / systemd timer): pre-open predict, post-close label.
  - REST API cleanly separated from the UI (so a React SPA could be added later for
    a public site without touching trading logic).
- **Frontend — HTMX + Jinja + Alpine.js + Tailwind** (server-rendered, no JS build)
  - Dashboard: arrow-on-bar gauge + factor breakdown + today's events/news + verdict.
  - History & accuracy: predicted vs. realized, calibration, signal hit-rate.
  - Settings/admin: edit weights, thresholds, API keys, toggle factors, set schedule.
- **Storage — SQLite** tables: `predictions`, `features`, `outcomes`, `news_items`,
  `news_scores`, `config`, `model_versions`.
- **Time:** everything anchored to `America/New_York` (9:30–16:00 ET), explicit DST
  handling independent of the host clock.

### Tentative repo structure
```
app/
  main.py                # FastAPI app + scheduler wiring
  config.py              # config layer (DB-backed, runtime-editable)
  db.py                  # SQLite models / migrations
  providers/
    base.py              # interfaces
    prices_yfinance.py   # free price/VIX/futures
    calendar_free.py     # free economic calendar
    news_gdelt.py        # free news
    llm_openai.py        # GPT scoring
  factors/               # one module per factor, each with a weight
  scoring/
    rules.py             # Phase 1 weighted-rule engine
    model.py             # Phase 2+ ML model + calibration
  labeling/
    efficiency.py        # ER / ADX / range label after close
  jobs/
    predict.py           # pre-open
    label.py             # post-close
  web/
    templates/           # Jinja + HTMX
    static/
data/                    # SQLite DB, cached bars
tests/
PLAN.md
README.md
```

---

## 5. Phases & deliverables

| Phase | Deliverable |
|------|-------------|
| 0 | Repo, config layer, SQLite schema, timezone handling, price/VIX/futures fetchers |
| 1 | **Rule-based scale + gauge UI — works day one** (improved FF rule) |
| 2 | Prediction logging + post-close ER/ADX labeling + accuracy dashboard |
| 3 | 2-yr hourly backfill + trained, calibrated LightGBM P(chop) model |
| 4 | GDELT + GPT news/geopolitics scoring layer |
| 5 | Scheduled retraining, calibration & drift monitoring |
| 6 | Public-website integration (reuse the FastAPI API) |

---

## 6. Accuracy roadmap (recommendations)
1. VIX + term structure (biggest lift over a pure calendar).
2. Overnight futures gap/range (reads the session before it opens).
3. Mega-cap earnings (critical for NASDAQ).
4. Structural days (OPEX, quad-witching, month/quarter-end, holidays/half-days).
5. Predict **chop**, not raw volatility (matches the strategy).
6. Bootstrap with history; don't wait years to learn.
7. Calibrate probabilities; walk-forward validation only.
8. Learn per event-type (e.g. FOMC may chop, ISM may trend — let data decide).
9. Ultimate label = the user's own trade results, if feedable later.

---

## 7. Honest caveats
- News/geopolitics scoring is directional, not precise.
- ForexFactory has no official API; scraping is fragile/ToS-risky — prefer a real
  calendar API.
- Markets are non-stationary — ongoing recalibration, not "train once."
- Intraday backfill depth is the main data constraint (free history is short).
- This is decision support, **not** financial advice.

---

## 8. Open / future
- Optional paid intraday history for a deeper backfill.
- Optional paid news feed (Marketaux / Benzinga / FMP) via `NewsProvider`.
- Feed real trade P&L as the learning label.
- React SPA for a polished public site (backend already API-first).
