# Productisation — backend for the separate frontend

This backend now serves two front-ends off **one shared engine**:

1. **`/api/v1/*`** — a read-only JSON API for the separate **Next.js frontend** (its own
   repo, deployed to Vercel). This is what the product dashboard consumes.
2. **The Jinja UI** (`/`, `/history`, `/settings`) — kept as an **internal admin / testing
   view**. It is unchanged; it now shares its presentation logic with the API so the two
   can never show different numbers.

The analysis is a **singleton** (the same NQ/SPX verdict for everyone), so the API takes no
per-user context. User accounts / billing sit *in front of* this later without touching the
engine.

```
   Users' browser
        |
        v
   Vercel  ── Next.js dashboard (separate repo, you own it)
        |  server-side fetch, Authorization: Bearer $API_KEY  (key never in the browser)
        v
   Railway / Render / Fly ── this FastAPI backend
        ├─ /api/v1/*   JSON (API-key auth)
        ├─ Jinja UI    admin/testing (HTTP Basic auth)
        ├─ APScheduler predict 08:45 ET / label 16:20 ET
        └─ SQLite on a persistent volume (TRADESCALE_DB)
```

## API contract (`/api/v1`)

All endpoints are GET and require the API key when `API_KEY` is set. Interactive docs at
`/docs`, machine-readable schema at `/openapi.json`.

| Endpoint            | Returns |
|---------------------|---------|
| `GET /today`        | Frozen morning verdict: tier, gauge (needle + raw dq + learned discount), news, factors, events. `has_prediction=false` until the day's prediction runs. |
| `GET /live`         | Live intraday tracker. Poll while `state == "live"`. |
| `GET /history?limit=` | Recent predictions joined with realized outcomes (raw scores, matching the admin History page). |
| `GET /accuracy`     | Win-rate / track record over graded sessions. |
| `GET /calibration`  | Learned VETO/WARN discount multipliers per tier + event category. |
| `GET /health`       | On-demand probe of the scraped feeds (makes live upstream calls). |

`GET /healthz` (no auth, no upstream calls) is a cheap liveness ping for the host.

### Auth & hardening
- **`API_KEY`** guards `/api/v1/*`. Send `Authorization: Bearer <key>` or `X-API-Key: <key>`.
- **`ADMIN_USER` / `ADMIN_PASS`** guard the Jinja UI via HTTP Basic.
- **`FRONTEND_ORIGINS`** — comma-separated CORS allowlist (only needed for direct browser
  calls; server-side calls from Next.js don't need it).
- **Rate limiting** — per-IP caps (`RATELIMIT_ADMIN` default 30/60, `RATELIMIT_API` default
  240/60); over the cap returns `429` with `Retry-After`. Tight on the admin surface
  (brute-force defence), generous on the API (traffic shares the frontend server's IPs).
  `/healthz` and `/static` are exempt.
- **HSTS** — `Strict-Transport-Security` (1 year) is sent on HTTPS responses only, so plain
  `http://localhost` dev is unaffected. Plus the existing CSP / anti-clickjacking headers.
- **Auth/CORS are optional**: unset ⇒ that gate is open, so local dev / the dev box behave as
  before. Rate limiting and HSTS are always on (HSTS only advertised over HTTPS).

## Running

- **Local (unchanged):** `.venv\Scripts\python.exe run.py` → http://localhost:8000. No env
  vars ⇒ everything open, the exact setup used for testing today.
- **Container:** `docker build -t tradescale . && docker run -p 8000:8000 --env-file .env tradescale`

## Deploy (Railway — recommended for the beta)

1. New project → deploy from the GitHub repo. Railway builds the `Dockerfile` (or Nixpacks
   from `requirements.txt` + `Procfile`).
2. Add a **Volume** mounted at `/data`.
3. Set env vars: `API_KEY`, `ADMIN_USER`, `ADMIN_PASS`, `FRONTEND_ORIGINS`,
   `TRADESCALE_DB=/data/tradescale.db`, and optionally `OPENAI_API_KEY`. (`.env.example` lists them.)
4. First boot: `init_db` runs, the 161-day news cache seeds from the committed
   `data/news_seed.csv`, and the scheduler starts. The first `/today` returns
   `has_prediction=false` until predict fires (08:45 ET) or you POST `/run-predict` from the
   admin UI.
5. Point the Next.js frontend's server env at the Railway URL + `API_KEY`.

Render / Fly work the same way (any always-on container host with a persistent disk).

## Deploy caveats (by design, for the beta)
- **Single instance only.** The scheduler is in-process; 2+ instances would double-fire the
  predict/label jobs. Fine for the beta — split the scheduler into its own worker before scaling.
- **SQLite needs the volume.** Without `TRADESCALE_DB` on a mounted disk, the DB is wiped on
  every redeploy. Migrating to Postgres is the natural next step once concurrency matters.
- **Free feeds are unofficial.** yfinance / ForexFactory are scrapers with no SLA and
  non-commercial terms — swap to licensed feeds before charging money. `GET /health` surfaces a
  silent scrape break.

## Not in this backend (frontend / later phases)
- User accounts, sessions, subscriptions (you build these with the dashboard).
- Stripe billing, "not financial advice" terms.
- Postgres, split scheduler worker.
