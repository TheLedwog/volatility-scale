"""FastAPI app: dashboard, history, settings + run endpoints."""
from __future__ import annotations

import html
import json
import os
import threading
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api.routes import router as api_router
from .api.ratelimit import ADMIN_LIMIT, API_LIMIT, check as ratelimit_check, client_ip
from .api.security import admin_credentials, check_basic_auth
from .config import (
    get_config,
    openai_api_key as resolve_openai_key,
    openai_key_status,
    reset,
    set_section,
)
from .db import init_db
from .labeling.efficiency import run_labeling, run_regrade
from .scoring.calibration import calibrate
from .scoring.engine import run_prediction
from .scoring.live import live_session
from .service.verdict import (
    computed_at_str,
    display_state,
    news_blind,
    overall_score,
)
from .store import (
    accuracy_summary,
    latest_model,
    latest_prediction,
    prediction_for,
    recent_history,
)
from .timeutils import today_et

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "web" / "templates"))

app = FastAPI(title="Trade / Don't-Trade Scale")
app.mount("/static", StaticFiles(directory=str(BASE / "web" / "static")), name="static")

# CORS for the separate frontend (e.g. Vercel). Origins come from FRONTEND_ORIGINS
# (comma-separated); empty = no cross-origin allowed. Server-side calls from the
# Next.js host don't need CORS, so this is belt-and-suspenders for browser calls.
_origins = [o.strip() for o in os.environ.get("FRONTEND_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
        allow_credentials=False,
    )

# The JSON product API (/api/v1/*), guarded by its own API key (see api/security.py).
app.include_router(api_router)

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
    # HSTS: force HTTPS for a year, but only advertise it on connections that are
    # actually HTTPS (the proxy sets X-Forwarded-Proto). Browsers ignore HSTS sent
    # over plain HTTP anyway, so this just keeps local http://localhost dev clean.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if proto == "https":
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


# Paths exempt from the admin HTTP-Basic gate: the JSON API (its own key auth),
# static assets, the OpenAPI docs, and the liveness ping.
_ADMIN_PUBLIC = ("/api/", "/static/", "/docs", "/redoc", "/openapi.json",
                 "/healthz", "/favicon")


@app.middleware("http")
async def _admin_auth(request: Request, call_next):
    """HTTP Basic gate for the Jinja admin/testing UI. Open in local dev (no
    ADMIN_USER/ADMIN_PASS set); required once they are, e.g. on the cloud host.
    The product API under /api/ is exempt - it uses its own API-key auth."""
    if admin_credentials() and not request.url.path.startswith(_ADMIN_PUBLIC):
        if not check_basic_auth(request.headers.get("Authorization")):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="tradescale admin"'},
            )
    return await call_next(request)


# Cheap liveness + static assets are never rate limited.
_RATELIMIT_EXEMPT = ("/static/", "/healthz", "/favicon")


@app.middleware("http")
async def _rate_limit(request: Request, call_next):
    """Per-IP request cap. A generous bucket for the API (traffic shares the
    frontend server's IPs) and a tight one for the admin surface (brute-force
    defence). Defined last so it runs first - abusive requests are rejected before
    any auth check or handler work."""
    path = request.url.path
    if not path.startswith(_RATELIMIT_EXEMPT):
        if path.startswith("/api/"):
            bucket, (max_n, window) = "api", API_LIMIT
        else:
            bucket, (max_n, window) = "admin", ADMIN_LIMIT
        allowed, retry_after = ratelimit_check(bucket, client_ip(request), max_n, window)
        if not allowed:
            return JSONResponse(
                {"detail": "Too many requests"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
    return await call_next(request)


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
        # One-off per row: stamp the as-of gate multiplier onto predictions stored
        # before it was frozen, so history replays the score each day actually showed
        # instead of re-deriving it from calibration that has since moved on.
        from .scoring.calibration import backfill_gate_multipliers
        n = backfill_gate_multipliers(get_config())
        if n:
            print(f"[startup] stamped gate multiplier on {n} stored predictions")
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] gate-multiplier backfill skipped: {exc}")
    try:
        # Warm the calendar cache so a fresh deploy (empty volume) can serve /calendar
        # and gate a prediction immediately, instead of waiting for the first scheduled
        # refresh. No-ops when the cache is already fresh.
        from .jobs.calendar_refresh import ensure_calendar
        r = ensure_calendar(get_config())
        print(f"[startup] calendar cache: {r}")
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] calendar warm-up skipped: {exc}")
    try:
        from .scheduler import start_scheduler
        start_scheduler()
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] scheduler not started: {exc}")


@app.get("/")
def dashboard(request: Request):
    cfg = get_config()
    pred = latest_prediction()
    state = display_state(pred, cfg) if pred else None
    live = live_session(cfg, prediction_for(today_et().isoformat()))
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"pred": pred, "state": state, "cfg": cfg, "live": live,
         "computed_at": computed_at_str(pred),
         "overall": overall_score(pred, cfg) if pred else None,
         "news_blind": news_blind(pred, cfg) if pred else False},
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


@app.post("/run-calendar-refresh")
def run_calendar_refresh():
    # Force an upstream calendar pull (the scheduled job is the normal path). Handy
    # after a feed outage, or to pick up next week the moment ForexFactory rolls over.
    from .jobs.calendar_refresh import refresh_calendar
    refresh_calendar()
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/run-regrade")
def run_regrade_route():
    # Re-label the recent stored sessions with the current thresholds/blend so the
    # track record + calibration reflect the live definition (yfinance 5-min data
    # limits this to ~60 days). Safe to click after changing the label cutoffs.
    run_regrade()
    return RedirectResponse(url="/history", status_code=303)


@app.get("/history")
def history(request: Request, training: str | None = None):
    return templates.TemplateResponse(
        request, "history.html",
        {"rows": recent_history(60), "acc": accuracy_summary(),
         "model": latest_model(), "training": training,
         "cal": calibrate(get_config())},
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
    from .service.calendar_view import calendar_status
    return templates.TemplateResponse(
        request, "settings.html",
        {
            "cfg": cfg,
            "key_status": openai_key_status(cfg),
            "calendar_status": calendar_status(cfg),
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


@app.get("/settings/check-data")
def settings_check_data(request: Request):
    """Live health check of the scraped feeds (yfinance + calendar), for Settings."""
    from .providers.health import check_all_feeds
    result = check_all_feeds(get_config())
    return templates.TemplateResponse(request, "_datacheck.html", {"result": result})


@app.post("/settings/reset")
def settings_reset():
    reset()
    return RedirectResponse(url="/settings?saved=reset", status_code=303)


@app.get("/healthz")
def healthz():
    """Cheap liveness ping for the host's health check (no data-feed calls, no auth)."""
    return {"status": "ok"}
