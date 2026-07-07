"""The /api/v1 router: a read-only JSON view of the shared engine.

The analysis is a singleton (same NQ/SPX verdict for everyone), so these endpoints
take no user context - user accounts / subscriptions sit in front of this later.
Mutations (predict/label/train/settings) stay on the admin UI, not the public API.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..config import get_config
from ..providers.health import check_all_feeds
from ..scoring.calibration import calibrate
from ..scoring.live import live_session
from ..store import accuracy_summary, latest_prediction, prediction_for, recent_history
from ..timeutils import today_et
from . import schemas, serializers
from .security import require_api_key

router = APIRouter(prefix="/api/v1", tags=["v1"], dependencies=[Depends(require_api_key)])


@router.get("/today", response_model=schemas.TodayResponse)
def today():
    """The frozen morning verdict (the hero card). `has_prediction` is false until
    the day's prediction has run."""
    cfg = get_config()
    return serializers.serialize_today(latest_prediction(), cfg)


@router.get("/live", response_model=schemas.LiveResponse)
def live():
    """The live intraday session tracker - poll this while `state == 'live'`."""
    cfg = get_config()
    return live_session(cfg, prediction_for(today_et().isoformat()))


@router.get("/history", response_model=schemas.HistoryResponse)
def history(limit: int = Query(60, ge=1, le=1000)):
    """Recent predictions joined with their realized outcomes, newest first."""
    rows = recent_history(limit)
    return {"count": len(rows), "rows": rows}


@router.get("/accuracy", response_model=schemas.AccuracyResponse)
def accuracy():
    """Win-rate / track record over the graded sessions."""
    return accuracy_summary()


@router.get("/calibration")
def calibration():
    """The learned VETO/WARN discount multipliers + supporting stats (per tier and
    event category). Returned as-is; the shape mirrors app.scoring.calibration."""
    return calibrate(get_config())


@router.get("/health", response_model=schemas.HealthResponse)
def health():
    """On-demand probe of the scraped data feeds (yfinance + calendar). This makes
    live upstream calls; use `/healthz` for a cheap liveness ping."""
    result = check_all_feeds(get_config())
    return {"status": "ok", **result}
