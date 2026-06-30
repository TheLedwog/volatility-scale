"""FastAPI app: dashboard, history, settings + run endpoints."""
from __future__ import annotations

import html
import json
import threading
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import (
    get_config,
    openai_api_key as resolve_openai_key,
    openai_key_status,
    reset,
    set_section,
)
from .db import init_db
from .labeling.efficiency import run_labeling
from .scoring.engine import run_prediction
from .scoring.live import live_session
from .store import (
    accuracy_summary,
    latest_model,
    latest_prediction,
    prediction_for,
    recent_history,
)
from .timeutils import fmt_et, today_et

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "web" / "templates"))

app = FastAPI(title="Trade / Don't-Trade Scale")
app.mount("/static", StaticFiles(directory=str(BASE / "web" / "static")), name="static")

# Defence-in-depth response headers. The app ships no third-party/inline scripts,
# so script-src can stay locked to 'self'; inline style attributes (dynamic gauge /
# bar widths) need 'unsafe-inline' on style-src only. data: is allowed for the
# inline SVG favicon. These do not replace putting auth in front of the app.
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


@app.on_event("startup")
def _startup() -> None:
    init_db()
    try:
        from .ml.seed_news import seed_if_empty
        n = seed_if_empty()
        if n:
            print(f"[startup] seeded {n} cached news days from news_seed.csv")
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] news seed skipped: {exc}")
    try:
        from .scheduler import start_scheduler
        start_scheduler()
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] scheduler not started: {exc}")


def _computed_at_str(pred: dict | None) -> str | None:
    """Format a prediction's created_at as 'HH:MM ET' for the frozen-snapshot label."""
    if not pred or not pred.get("created_at"):
        return None
    try:
        from datetime import datetime
        return fmt_et(datetime.fromisoformat(pred["created_at"]))
    except (ValueError, TypeError):
        return None


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
    live = live_session(cfg, prediction_for(today_et().isoformat()))
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"pred": pred, "state": state, "cfg": cfg, "live": live,
         "computed_at": _computed_at_str(pred)},
    )


@app.get("/live-panel")
def live_panel(request: Request):
    """Live intraday tracker fragment, polled by the dashboard while market is open."""
    cfg = get_config()
    live = live_session(cfg, prediction_for(today_et().isoformat()))
    return templates.TemplateResponse(request, "_live.html", {"live": live})


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


# The "Advanced" card edits these structural sections as raw JSON. Everything
# else has friendly form controls. No section is edited by both mechanisms, so
# saves never clobber each other.
ADVANCED_SECTIONS = ("session", "tickers", "gate", "ml")
OPENAI_MODELS = ("gpt-4o-mini", "gpt-4o")
SCORING_MODES = ("auto", "rules", "model")


@app.get("/settings")
def settings_get(request: Request, saved: str | None = None, error: str | None = None):
    cfg = get_config()
    advanced = {k: json.dumps(cfg[k], indent=2) for k in ADVANCED_SECTIONS}
    return templates.TemplateResponse(
        request, "settings.html",
        {
            "cfg": cfg,
            "key_status": openai_key_status(cfg),
            "advanced": advanced,
            "models": OPENAI_MODELS,
            "modes": SCORING_MODES,
            "saved": saved,
            "error": error,
        },
    )


def _num(form, name, cast, default):
    raw = form.get(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


@app.post("/settings")
async def settings_post(request: Request):
    """Save the friendly form. Each section is merged onto current config so
    fields not shown here (e.g. provider keys, news provider) are preserved."""
    form = await request.form()
    cfg = get_config()

    set_section("scoring", {**cfg["scoring"],
                            "mode": form.get("scoring_mode", cfg["scoring"]["mode"])})

    news = {**cfg["news"], "enabled": "news_enabled" in form}
    news["max_headlines"] = _num(form, "news_max_headlines", int, news.get("max_headlines"))
    if form.get("news_query") is not None:
        news["query"] = form["news_query"]
    set_section("news", news)

    prov = {**cfg["providers"]}
    if form.get("openai_model"):
        prov["openai_model"] = form["openai_model"]
    set_section("providers", prov)

    thr = {**cfg["thresholds"]}
    thr["good"] = _num(form, "thr_good", int, thr.get("good"))
    thr["caution"] = _num(form, "thr_caution", int, thr.get("caution"))
    thr["dead_day_range_pct"] = _num(form, "thr_dead", float, thr.get("dead_day_range_pct"))
    thr["label_directional_er"] = _num(form, "thr_dir_er", float, thr.get("label_directional_er"))
    thr["label_choppy_er"] = _num(form, "thr_chop_er", float, thr.get("label_choppy_er"))
    set_section("thresholds", thr)

    sch = {**cfg["schedule"], "enabled": "sch_enabled" in form}
    if form.get("sch_predict_time"):
        sch["predict_time"] = form["sch_predict_time"]
    if form.get("sch_label_time"):
        sch["label_time"] = form["sch_label_time"]
    set_section("schedule", sch)

    w = {**cfg["weights"]}
    for name in w:
        w[name] = _num(form, "w_" + name, float, w[name])
    set_section("weights", w)

    return RedirectResponse(url="/settings?saved=1", status_code=303)


@app.post("/settings/advanced")
async def settings_advanced(request: Request):
    form = await request.form()
    for key in ADVANCED_SECTIONS:
        if key not in form:
            continue
        try:
            value = json.loads(form[key])
        except json.JSONDecodeError as exc:
            return RedirectResponse(url=f"/settings?error={key}: {exc}", status_code=303)
        set_section(key, value)
    return RedirectResponse(url="/settings?saved=adv", status_code=303)


@app.post("/settings/key")
async def settings_key(openai_api_key: str = Form("")):
    key = openai_api_key.strip()
    if not key:
        return RedirectResponse(url="/settings?error=No+key+entered.", status_code=303)
    cfg = get_config()
    set_section("providers", {**cfg["providers"], "openai_api_key": key})
    return RedirectResponse(url="/settings?saved=key", status_code=303)


@app.post("/settings/key/remove")
def settings_key_remove():
    cfg = get_config()
    set_section("providers", {**cfg["providers"], "openai_api_key": ""})
    return RedirectResponse(url="/settings?saved=keyremoved", status_code=303)


def _test_openai_key(key: str) -> tuple[bool, str]:
    """A no-cost auth check: list models. Returns (ok, message)."""
    try:
        from openai import OpenAI

        OpenAI(api_key=key, timeout=12.0, max_retries=0).models.list()
        return True, "Key works - OpenAI authenticated."
    except Exception as exc:  # noqa: BLE001
        msg = " ".join(str(exc).split())[:200]
        return False, f"Key failed: {msg}"


@app.post("/settings/test-key")
async def settings_test_key(openai_api_key: str = Form("")):
    cfg = get_config()
    key = openai_api_key.strip()
    if not key:  # nothing typed -> test whatever key is actually active
        key = resolve_openai_key(cfg)
    if not key:
        body = '<p class="alert alert-warn small">No key to test — paste one above or set OPENAI_API_KEY.</p>'
        return HTMLResponse(body)
    ok, msg = _test_openai_key(key)
    cls = "alert-ok" if ok else "alert-veto"
    icon = "&#10003;" if ok else "&#10007;"
    return HTMLResponse(f'<p class="alert {cls} small">{icon} {html.escape(msg)}</p>')


@app.post("/settings/reset")
def settings_reset():
    reset()
    return RedirectResponse(url="/settings?saved=reset", status_code=303)


@app.get("/api/status")
def api_status():
    return {"latest": latest_prediction(), "accuracy": accuracy_summary()}
