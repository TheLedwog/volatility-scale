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
    gauge: Gauge | None = None
    news: NewsBlock | None = None
    model: ModelInfo | None = None
    factors: list[dict] = []
    events: list[dict] = []


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
    direction_quality: int | None = None
    verdict: str | None = None
    reason: str | None = None
    realized_er: float | None = None
    realized_label: str | None = None
    range_pct: float | None = None


class HistoryResponse(BaseModel):
    count: int
    rows: list[HistoryRow]


class AccuracyResponse(BaseModel):
    samples: int
    good_threshold: int
    caution_threshold: int
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


class HealthResponse(BaseModel):
    status: str                      # app liveness
    ok: bool                         # all required feeds up
    degraded: bool                   # an optional feed (e.g. VIX3M) is down
    feeds: list[dict]
