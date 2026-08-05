"""Background runner for the admin actions (predict, label, re-grade, train, …).

The Jinja UI can POST these straight through because it answers with a 303 and the
browser waits. An API called from a Next.js route handler can't: `predict` makes live
yfinance and OpenAI calls and routinely takes tens of seconds, and `train` runs for
minutes. So a POST starts the work and returns a job id immediately, and the panel
polls for the outcome.

Single-flight per action: starting an action that is already running returns the
running job rather than a second one. Two concurrent predicts would race on the same
row, and two trains would fight over the model file.

Scope, so it isn't mistaken for a durable queue: this is in-process memory. A host
restart mid-run loses the record (the job dies with it either way), and it assumes
one instance - the same assumption the rate limiter and the scheduler already make.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone

_MAX_KEPT = 50          # completed job records retained for polling

_jobs: dict[str, dict] = {}
_running: dict[str, str] = {}   # action -> job_id currently running
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── the actions ─────────────────────────────────────────────────────────────────
# Each returns a JSON-serialisable summary, which becomes the job's `result`.

def _predict() -> dict:
    from ..scoring.engine import run_prediction
    r = run_prediction()
    # A summary, not the whole prediction: the factor rows, headlines and events are
    # already served by /api/v1/today, and a job record is polled repeatedly.
    return {k: r.get(k) for k in ("date", "tier", "direction_quality", "verdict",
                                  "trade_ok", "dead_day", "calendar_unavailable")}


def _label() -> dict:
    from ..labeling.efficiency import run_labeling
    r = run_labeling()
    return r if isinstance(r, dict) else {"ok": True}


def _regrade() -> dict:
    from ..labeling.efficiency import run_regrade
    r = run_regrade()
    return r if isinstance(r, dict) else {"ok": True}


def _train() -> dict:
    from ..ml.build import run as build_run
    r = build_run()
    return r if isinstance(r, dict) else {"ok": True}


def _calendar_refresh() -> dict:
    from ..jobs.calendar_refresh import refresh_calendar
    r = refresh_calendar()
    return r if isinstance(r, dict) else {"ok": True}


ACTIONS: dict[str, dict] = {
    "predict": {"fn": _predict,
                "label": "Re-run the prediction",
                "description": "Recompute today's verdict now. Note this REPLACES the "
                               "frozen call cut at the open, using current data."},
    "label": {"fn": _label,
              "label": "Grade the session",
              "description": "Grade the most recent completed session's realized "
                             "efficiency ratio."},
    "regrade": {"fn": _regrade,
                "label": "Re-grade stored sessions",
                "description": "Re-label recent stored sessions with the current "
                               "cutoffs. yfinance only serves ~60 days of 5-min bars, "
                               "so older rows keep their original grade."},
    "train": {"fn": _train,
              "label": "Train the model",
              "description": "Rebuild the dataset and retrain. Runs for minutes."},
    "calendar-refresh": {"fn": _calendar_refresh,
                         "label": "Refresh the calendar",
                         "description": "Force an upstream ForexFactory pull. The gate "
                                        "reads the cache, so this is how you recover "
                                        "from an outage without waiting for the "
                                        "scheduled run."},
}


def actions_payload() -> list[dict]:
    """The action catalogue, so the panel can render buttons without hard-coding them."""
    with _lock:
        return [
            {"action": name,
             "label": spec["label"],
             "description": spec["description"],
             "running": name in _running,
             "last": _summary(_jobs.get(_last_completed(name))) if _last_completed(name) else None}
            for name, spec in ACTIONS.items()
        ]


def _last_completed(action: str) -> str | None:
    done = [j for j in _jobs.values() if j["action"] == action and j["status"] != "running"]
    return max(done, key=lambda j: j["started_at"])["id"] if done else None


def _jsonable(value):
    """Best-effort JSON-safety for whatever an action hands back.

    The engine's dicts carry numpy scalars and dates in places; a job record that
    can't be serialised would turn a successful run into a 500 on the poll.
    """
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "item"):          # numpy scalar
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            pass
    return str(value)


def _summary(job: dict | None) -> dict | None:
    if not job:
        return None
    return {k: job.get(k) for k in ("id", "action", "status", "started_at",
                                    "finished_at", "duration_sec", "result", "error")}


def _prune_locked() -> None:
    done = sorted((j for j in _jobs.values() if j["status"] != "running"),
                  key=lambda j: j["started_at"])
    for job in done[:max(0, len(done) - _MAX_KEPT)]:
        _jobs.pop(job["id"], None)


def start(action: str) -> dict:
    """Start `action` in the background, or return the run already in flight.

    The returned dict always carries `already_running`, so the caller can tell a
    fresh start from a no-op without comparing job ids.
    """
    spec = ACTIONS.get(action)
    if not spec:
        raise KeyError(action)

    with _lock:
        existing = _running.get(action)
        if existing:
            return {**_summary(_jobs[existing]), "already_running": True}
        job_id = uuid.uuid4().hex[:12]
        _jobs[job_id] = {"id": job_id, "action": action, "status": "running",
                         "started_at": _now(), "finished_at": None,
                         "result": None, "error": None}
        _running[action] = job_id
        _prune_locked()

    def _run() -> None:
        started = time.monotonic()
        try:
            result = _jsonable(spec["fn"]())
            status, error = "ok", None
        except Exception as exc:  # noqa: BLE001 - surfaced on the job, never raised here
            result, status = None, "error"
            error = " ".join(str(exc).split())[:500]
            print(f"[jobs] {action} failed: {error}")
        with _lock:
            _jobs[job_id].update(
                status=status, result=result, error=error, finished_at=_now(),
                duration_sec=round(time.monotonic() - started, 1),
            )
            if _running.get(action) == job_id:
                _running.pop(action, None)

    threading.Thread(target=_run, name=f"job-{action}-{job_id}", daemon=True).start()
    with _lock:
        return {**_summary(_jobs[job_id]), "already_running": False}


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None
