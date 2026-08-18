"""Pydantic response models = the versioned API contract the frontend builds against.

Nested engine internals whose shape can vary by scoring mode (factor rows, calendar
events) are typed as free-form dicts on purpose, so a model-mode breakdown can't
500 the endpoint; the load-bearing top-level shape is pinned.
"""
from __future__ import annotations

from pydantic import BaseModel


class Gauge(BaseModel):
    overall: int | None            # needle 0..100, learned gate discount folded in
    direction_quality: int | None  # raw score before the gate
    multiplier: float              # discount applied (1.0 = none)
    tier_discounted: bool          # True on VETO/WARN days


class NewsBlock(BaseModel):
    enabled: bool
    scored: bool
    blind: bool
    direction: str | None = None
    expected_impact: float | None = None
    chop_risk: float | None = None
    rationale: str | None = None
    headlines: list[str] = []
    source: str | None = None
    error: str | None = None


class ModelInfo(BaseModel):
    mode: str                      # scoring.mode: auto | rules | model
    kind: str                      # what actually produced the score: rules | model
    note: str | None = None


class Tradeability(BaseModel):
    """The tradeability engine's read (app/scoring/tradeability.py).

    ADDITIVE - present alongside every existing field, never replacing one. While
    `mode` is "shadow" this is informational only and `gauge` still carries the rule
    engine's score; when it is "live" `score` here equals `gauge.direction_quality`.
    """
    mode: str                          # shadow | live
    score: int | None = None           # 1..100 percentile of expected net move
    band: str | None = None            # thin | normal | best
    expected_net_pct: float | None = None      # forecast |close-open| as % of open
    expected_range_pct: float | None = None    # forecast high-low as % of open
    expected_efficiency: float | None = None   # forecast share of range kept as net
    fitted_at: str | None = None       # when the model behind this was fitted
    n_samples: int | None = None       # sessions it was fitted on
    error: str | None = None


class TodayResponse(BaseModel):
    has_prediction: bool
    date: str | None = None
    computed_at: str | None = None   # 'HH:MM ET' the frozen call was made
    tier: str | None = None          # VETO | WARN | CLEAN | CLOSED
    verdict: str | None = None       # human headline
    state: str | None = None         # veto|warn|good|mixed|avoid|closed (drives UI colour)
    trade_ok: bool | None = None
    reason: str | None = None
    warn_note: str | None = None
    dead_day: bool = False
    # True when the calendar could not be loaded for this day, so scheduled events were
    # never checked. A CLEAN tier alongside this means "nothing KNOWN", not "nothing on".
    calendar_unavailable: bool = False
    gauge: Gauge | None = None
    news: NewsBlock | None = None
    model: ModelInfo | None = None
    factors: list[dict] = []
    events: list[dict] = []
    # Both additive. `factors` above keeps the rule-engine rows in every mode, so an
    # existing client is untouched; `legs` is the tradeability breakdown to migrate to.
    tradeability: Tradeability | None = None
    legs: list[dict] = []


class CalendarStatus(BaseModel):
    """Provenance of the cached calendar - how fresh the events below actually are."""
    fetched_at: str | None = None    # last SUCCESSFUL upstream fetch
    attempted_at: str | None = None  # last attempt, success or not
    ok: bool | None = None           # did the last attempt succeed
    error: str | None = None
    age_hours: float | None = None
    stale: bool
    never_fetched: bool


class CalendarDay(BaseModel):
    date: str
    weekday: str
    is_trading_day: bool
    tier: str                        # VETO | WARN | CLEAN - the gate, calendar-only
    reason: str = ""                 # why it's a VETO
    warn_note: str = ""              # why it's a WARN
    events: list[dict] = []


class CalendarResponse(BaseModel):
    """The day's (or the week ahead's) economic calendar + the tier it implies.

    Available from 00:00 ET, long before the prediction runs, because the gate needs no
    price data. `mode` is "today" on a normal day and "week_ahead" once the week's last
    session has closed (Friday evening + the weekend).
    """
    mode: str                        # today | week_ahead
    generated_at: str
    today: str
    calendar: CalendarStatus
    # week_ahead with no events = ForexFactory hasn't published the new week yet (its
    # feed only ever holds one week). Distinct from "next week is quiet".
    awaiting_feed: bool
    event_count: int
    days: list[CalendarDay]


class LiveResponse(BaseModel):
    state: str                       # pre_open|waiting|live|after_close|closed_day|error
    er: float | None = None
    dq: int | None = None
    label: str | None = None         # TRENDING | MIXED | CHOPPY
    pct_move: float | None = None
    bars: int | None = None
    as_of: str | None = None
    open_str: str | None = None
    error: str | None = None
    call_check: dict | None = None


class HistoryRow(BaseModel):
    date: str
    tier: str
    direction_quality: int | None = None   # raw score, before the gate discount
    overall: int | None = None             # score the gauge showed (gate folded in)
    verdict: str | None = None
    reason: str | None = None
    realized_er: float | None = None
    realized_label: str | None = None
    range_pct: float | None = None
    # Additive: what the tradeability engine said that day (shadow or live), so the
    # two scores can be compared over the same sessions.
    tradeability_score: int | None = None
    # Realised net move |close-open| as % of open - the target the tradeability score
    # predicts. Filled in by the labeler; None for sessions graded before it existed.
    realized_net_pct: float | None = None


class HistoryResponse(BaseModel):
    count: int
    rows: list[HistoryRow]


class BasisStats(BaseModel):
    """Win rates for one scoring basis (see AccuracyResponse.basis)."""
    go_n: int
    go_win_rate: float | None = None
    avoid_n: int
    avoid_hit_rate: float | None = None
    overall_n: int
    overall_win_rate: float | None = None
    avg_er_good: float | None = None
    avg_er_avoid: float | None = None


class AccuracyResponse(BaseModel):
    samples: int
    good_threshold: int
    caution_threshold: int
    # Which number the headline rates grade. "overall" = the score the gauge showed,
    # gate folded in, gated days included - what a user actually acts on.
    basis: str = "overall"
    avg_er_good: float | None = None
    avg_er_avoid: float | None = None
    veto_days: int
    veto_chop_rate: float | None = None
    go_n: int
    go_win_rate: float | None = None
    avoid_n: int
    avoid_hit_rate: float | None = None
    overall_n: int
    overall_win_rate: float | None = None
    # Same rates on the raw pre-gate score, for comparing the two bases.
    raw_score: BasisStats | None = None


class HealthResponse(BaseModel):
    status: str                      # app liveness
    ok: bool                         # all required feeds up
    degraded: bool                   # an optional feed (e.g. VIX3M) is down
    feeds: list[dict]
