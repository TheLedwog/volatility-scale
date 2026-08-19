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


def _safe_calendar():
    try:
        from .jobs.calendar_refresh import refresh_calendar
        r = refresh_calendar()
        if not r.get("ok"):
            print(f"[scheduler] calendar refresh failed: {r.get('error')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] calendar refresh failed: {exc}")


def _safe_news():
    try:
        from .jobs.news_refresh import refresh_news
        r = refresh_news()
        if not r.get("ok"):
            print(f"[scheduler] news warm-up not scored yet: {r}")
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] news refresh failed: {exc}")


def _safe_tradeability_refit():
    """Weekly refit of the tradeability model.

    This is the loop the old ML path never had: its Spearman was frozen inside a
    joblib file that only a manual retrain ever touched, so the model could never
    improve from accumulating data. Here the fit and the reference distribution both
    re-derive from upstream history on a schedule, so the scale keeps re-basing itself
    to the current volatility regime without anyone clicking anything.
    """
    try:
        from .config import get_config
        from .providers import get_price_provider
        from .scoring.tradeability import refit
        cfg = get_config()
        if not cfg.get("tradeability", {}).get("enabled", False):
            return
        r = refit(cfg, get_price_provider())
        if r.get("ok"):
            print(f"[scheduler] tradeability refit ok - {r['n_samples']} sessions "
                  f"through {r['date_to']}")
        else:
            print(f"[scheduler] tradeability refit failed: {r.get('error')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] tradeability refit failed: {exc}")


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

    predict_t = parse_hhmm(sch_cfg.get("predict_time", "09:30"))
    label_t = parse_hhmm(sch_cfg.get("label_time", "16:20"))
    cal_times = sch_cfg.get("calendar_refresh_times") or ["06:00", "12:00", "18:00", "22:00"]
    news_times = sch_cfg.get("news_refresh_times") or ["07:30", "08:30", "09:20"]

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
    # Every day, not mon-fri: the weekend runs catch FF's rollover to the new week,
    # which is the only way next week's events ever reach the cache.
    for i, raw in enumerate(cal_times):
        try:
            t = parse_hhmm(raw)
        except Exception:  # noqa: BLE001 - a bad time in Settings shouldn't kill the app
            print(f"[scheduler] bad calendar_refresh_time {raw!r}, skipped")
            continue
        _scheduler.add_job(
            _safe_calendar,
            CronTrigger(hour=t.hour, minute=t.minute, timezone=ET),
            id=f"calendar_{i}", replace_existing=True,
        )

    # Warm the news read before the open (mon-fri). Each run no-ops once the day is
    # cached and scored, so several attempts cost one upstream call - which is the
    # point: a 429 at 09:30 used to blank the factor for the whole session.
    for i, raw in enumerate(news_times):
        try:
            t = parse_hhmm(raw)
        except Exception:  # noqa: BLE001 - a bad time in Settings shouldn't kill the app
            print(f"[scheduler] bad news_refresh_time {raw!r}, skipped")
            continue
        _scheduler.add_job(
            _safe_news,
            CronTrigger(day_of_week="mon-fri", hour=t.hour, minute=t.minute, timezone=ET),
            id=f"news_{i}", replace_existing=True,
        )

    # Refit the tradeability model weekly, on Saturday, well clear of a session. The
    # predict path also refits on demand when the stored model ages past
    # tradeability.refit_days, so this is the tidy path rather than the only one.
    _scheduler.add_job(
        _safe_tradeability_refit,
        CronTrigger(day_of_week="sat", hour=5, minute=30, timezone=ET),
        id="tradeability_refit", replace_existing=True,
    )

    _scheduler.start()
    print(f"[scheduler] started - predict {predict_t:%H:%M} ET, label {label_t:%H:%M} ET, "
          f"calendar {', '.join(cal_times)} ET (daily), news {', '.join(news_times)} ET, "
          f"tradeability refit Sat 05:30 ET")
    return _scheduler


def stop_scheduler() -> bool:
    """Shut the running scheduler down. Returns True if there was one."""
    global _scheduler
    if _scheduler is None:
        return False
    try:
        _scheduler.shutdown(wait=False)
    except Exception as exc:  # noqa: BLE001 - already stopped, or never started
        print(f"[scheduler] shutdown: {exc}")
    _scheduler = None
    return True


def restart_scheduler():
    """Rebuild the cron triggers from the current config.

    The triggers are only read at startup, so without this a schedule change saved
    from the admin API would sit in the database looking applied while the old times
    kept firing until the next redeploy. Returns the new scheduler, or None when the
    schedule is disabled (in which case the old one is simply stopped).
    """
    stop_scheduler()
    return start_scheduler()


def scheduler_status() -> dict:
    """What the scheduler is actually going to do next, for the admin panel."""
    if _scheduler is None:
        return {"running": False, "jobs": []}
    jobs = []
    for job in _scheduler.get_jobs():
        nxt = getattr(job, "next_run_time", None)
        jobs.append({"id": job.id, "next_run": nxt.isoformat() if nxt else None})
    return {"running": bool(getattr(_scheduler, "running", False)),
            "jobs": sorted(jobs, key=lambda j: j["next_run"] or "")}
