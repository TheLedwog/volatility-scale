"""In-process scheduler (APScheduler). Runs the predict/label jobs at the times
set in Settings, in ET. On a Pi you can disable this and use cron instead.
"""
from __future__ import annotations

from .config import get_config
from .timeutils import ET, parse_hhmm

_scheduler = None


def _safe_predict():
    try:
        from .scoring.engine import run_prediction
        run_prediction()
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] predict failed: {exc}")


def _safe_label():
    try:
        from .labeling.efficiency import run_labeling
        run_labeling()
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] label failed: {exc}")


def start_scheduler():
    global _scheduler
    cfg = get_config()
    sch_cfg = cfg["schedule"]
    if not sch_cfg.get("enabled", True):
        return None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] APScheduler unavailable, skipping: {exc}")
        return None

    predict_t = parse_hhmm(sch_cfg.get("predict_time", "08:45"))
    label_t = parse_hhmm(sch_cfg.get("label_time", "16:20"))

    _scheduler = BackgroundScheduler(timezone=ET)
    _scheduler.add_job(
        _safe_predict, CronTrigger(day_of_week="mon-fri",
                                   hour=predict_t.hour, minute=predict_t.minute, timezone=ET),
        id="predict", replace_existing=True,
    )
    _scheduler.add_job(
        _safe_label, CronTrigger(day_of_week="mon-fri",
                                 hour=label_t.hour, minute=label_t.minute, timezone=ET),
        id="label", replace_existing=True,
    )
    _scheduler.start()
    print(f"[scheduler] started - predict {predict_t:%H:%M} ET, label {label_t:%H:%M} ET")
    return _scheduler
