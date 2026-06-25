"""FastAPI app: dashboard, history, settings + run endpoints."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import EDITABLE_SECTIONS, get_config, reset, set_section
from .db import init_db
from .labeling.efficiency import run_labeling
from .scoring.engine import run_prediction
from .store import accuracy_summary, latest_model, latest_prediction, recent_history

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "web" / "templates"))

app = FastAPI(title="Trade / Don't-Trade Scale")
app.mount("/static", StaticFiles(directory=str(BASE / "web" / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    init_db()
    try:
        from .scheduler import start_scheduler
        start_scheduler()
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] scheduler not started: {exc}")


def _display_state(pred: dict, cfg: dict) -> str:
    tier = pred["tier"]
    if tier in ("VETO", "CLOSED"):
        return tier.lower()
    if tier == "WARN":
        return "warn"
    dq = pred.get("direction_quality") or 0
    if dq >= cfg["thresholds"]["good"]:
        return "good"
    if dq < cfg["thresholds"]["caution"]:
        return "avoid"
    return "mixed"


@app.get("/")
def dashboard(request: Request):
    cfg = get_config()
    pred = latest_prediction()
    state = _display_state(pred, cfg) if pred else None
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"pred": pred, "state": state, "cfg": cfg},
    )


@app.post("/run-predict")
def run_predict():
    run_prediction()
    return RedirectResponse(url="/", status_code=303)


@app.post("/run-label")
def run_label():
    run_labeling()
    return RedirectResponse(url="/history", status_code=303)


@app.get("/history")
def history(request: Request, training: str | None = None):
    return templates.TemplateResponse(
        request, "history.html",
        {"rows": recent_history(60), "acc": accuracy_summary(),
         "model": latest_model(), "training": training},
    )


_train_lock = threading.Lock()


@app.post("/run-train")
def run_train():
    def _job():
        if not _train_lock.acquire(blocking=False):
            return
        try:
            from .ml.build import run as build_run
            build_run()
        except Exception as exc:  # noqa: BLE001
            print(f"[train] failed: {exc}")
        finally:
            _train_lock.release()

    threading.Thread(target=_job, daemon=True).start()
    return RedirectResponse(url="/history?training=1", status_code=303)


# Secrets are never echoed back to the UI; they show as MASK and are only
# overwritten when the user types a real new value.
SECRET_FIELDS = ("openai_api_key", "news_api_key")
MASK = "********"


@app.get("/settings")
def settings_get(request: Request, saved: str | None = None, error: str | None = None):
    cfg = get_config()
    sections = {}
    for key in EDITABLE_SECTIONS:
        data = cfg[key]
        if key == "providers":
            data = dict(data)
            for sf in SECRET_FIELDS:
                if data.get(sf):
                    data[sf] = MASK
        sections[key] = json.dumps(data, indent=2)
    return templates.TemplateResponse(
        request, "settings.html",
        {"sections": sections, "saved": saved, "error": error},
    )


@app.post("/settings")
async def settings_post(request: Request):
    form = await request.form()
    current = get_config()
    for key in EDITABLE_SECTIONS:
        if key not in form:
            continue
        try:
            value = json.loads(form[key])
        except json.JSONDecodeError as exc:
            return RedirectResponse(url=f"/settings?error={key}: {exc}", status_code=303)
        if key == "providers" and isinstance(value, dict):
            for sf in SECRET_FIELDS:
                # keep the stored secret if the field was left masked/blank
                if value.get(sf) in (MASK, None):
                    value[sf] = current["providers"].get(sf, "")
        set_section(key, value)
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@app.post("/settings/reset")
def settings_reset():
    reset()
    return RedirectResponse(url="/settings?saved=reset", status_code=303)


@app.get("/api/status")
def api_status():
    return {"latest": latest_prediction(), "accuracy": accuracy_summary()}
