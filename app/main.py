"""FastAPI app: dashboard, history, settings + run endpoints."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import EDITABLE_SECTIONS, get_config, reset, set_section
from .db import init_db
from .labeling.efficiency import run_labeling
from .scoring.engine import run_prediction
from .store import accuracy_summary, latest_prediction, recent_history

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
def history(request: Request):
    return templates.TemplateResponse(
        request, "history.html",
        {"rows": recent_history(60), "acc": accuracy_summary()},
    )


@app.get("/settings")
def settings_get(request: Request, saved: str | None = None, error: str | None = None):
    cfg = get_config()
    sections = {k: json.dumps(cfg[k], indent=2) for k in EDITABLE_SECTIONS}
    return templates.TemplateResponse(
        request, "settings.html",
        {"sections": sections, "saved": saved, "error": error},
    )


@app.post("/settings")
async def settings_post(request: Request):
    form = await request.form()
    for key in EDITABLE_SECTIONS:
        if key not in form:
            continue
        try:
            value = json.loads(form[key])
        except json.JSONDecodeError as exc:
            return RedirectResponse(url=f"/settings?error={key}: {exc}", status_code=303)
        set_section(key, value)
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@app.post("/settings/reset")
def settings_reset():
    reset()
    return RedirectResponse(url="/settings?saved=reset", status_code=303)


@app.get("/api/status")
def api_status():
    return {"latest": latest_prediction(), "accuracy": accuracy_summary()}
