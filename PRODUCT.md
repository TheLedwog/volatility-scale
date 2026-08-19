# Productisation — backend for the separate frontend

This backend serves the frontend off **one shared engine**, through three surfaces:

1. **`/api/v1/*`** — a read-only JSON API for the separate **Next.js frontend** (its own
   repo, deployed to Vercel). This is what the product dashboard consumes.
2. **`/api/v1/admin/*`** — the mutating settings API behind its **own key**, which the
   frontend's admin panel drives. Everything the Jinja settings page can do, as JSON.
3. **The Jinja UI** (`/`, `/history`, `/settings`) — kept as an **internal admin / testing
   view**. It is unchanged; it shares its presentation logic with the API so the two
   can never show different numbers.

The analysis is a **singleton** (the same NQ/SPX verdict for everyone), so the API takes no
per-user context. User accounts / billing sit *in front of* this later without touching the
engine.

```
   Users' browser
        |
        v
   Vercel  ── Next.js dashboard + admin panel (separate repo, you own it)
        |  server-side fetch, Authorization: Bearer $API_KEY        (dashboard)
        |  server-side fetch, Authorization: Bearer $ADMIN_API_KEY  (admin panel)
        |  neither key ever reaches the browser
        v
   Railway / Render / Fly ── this FastAPI backend
        ├─ /api/v1/*        JSON, read-only  (API_KEY)
        ├─ /api/v1/admin/*  JSON, mutating   (ADMIN_API_KEY)
        ├─ Jinja UI    admin/testing (HTTP Basic auth)
        ├─ APScheduler predict 09:30 ET / label 16:20 ET
        │              calendar refresh 06:00, 12:00, 18:00, 22:00 ET (every day)
        └─ SQLite on a persistent volume (TRADESCALE_DB)
```

## API contract (`/api/v1`)

All endpoints are GET and require the API key when `API_KEY` is set. Interactive docs at
`/docs`, machine-readable schema at `/openapi.json`.

| Endpoint            | Returns |
|---------------------|---------|
| `GET /today`        | Frozen verdict, cut at the open: tier, gauge (needle + raw dq + learned discount), news, factors, events. `has_prediction=false` until the day's prediction runs — use `/calendar` before then. |
| `GET /calendar`     | **The day's events + the tier they imply, from 00:00 ET.** Flips to the week ahead after the week's last session. See below. |
| `GET /live`         | Live intraday tracker. Poll while `state == "live"`. |
| `GET /history?limit=` | Recent predictions joined with realized outcomes (raw scores, matching the admin History page). |
| `GET /accuracy`     | Win-rate / track record over graded sessions. |
| `GET /calibration`  | Learned VETO/WARN discount multipliers per tier + event category. |
| `GET /health`       | On-demand probe of the scraped feeds (makes live upstream calls). |

`GET /healthz` (no auth, no upstream calls) is a cheap liveness ping for the host.

### `GET /calendar` — the day, hours before the score

The gate is **calendar-only**: whether a day is VETO / WARN / CLEAN needs no price data. So the
tier is knowable at **00:00 ET**, while the score isn't cut until the open. This endpoint hands the
frontend the whole day up front — "today is a VETO day, FOMC at 14:00" — and `/today` fills the
gauge in at 09:30.

Served **entirely from the local cache**, so polling it costs zero upstream calls.

- `mode: "today"` — a normal day; `days` holds one entry.
- `mode: "week_ahead"` — set once the week's **last session has closed** (Friday after 16:00 ET, and
  all weekend). `days` holds next week's Mon–Fri, each with its own tier, so the frontend can show
  "FOMC on Wednesday" over the weekend. Derived from the trading calendar, so a holiday-shortened
  week flips on Thursday's close instead.
- `awaiting_feed: true` — week-ahead, but ForexFactory hasn't published the new week yet (see the
  caveat below). Means "not out yet", **not** "next week is quiet".
- `calendar: {fetched_at, age_hours, stale, never_fetched, ok, error}` — provenance of the cache.

Each day carries `tier`, `reason` (why VETO), `warn_note` (why WARN), and the enriched `events`
(`title`, `impact`, `time_str`, `category`, `category_label`, `intra_session`).

**Caveat — ForexFactory's week runs Sunday→Saturday.** Its free feed only ever holds the *current*
week (there is no `nextweek` feed; it 404s), so on **Friday evening next week does not exist upstream
yet** — it appears when the feed rolls over on **Sunday**. The Friday/Saturday week-ahead view is
therefore honest-but-empty (`awaiting_feed: true`) and fills itself in on Sunday, when a weekend
refresh picks the new week up.

## Admin API (`/api/v1/admin`) — settings on the frontend

The engine's settings, as JSON, for the admin panel in the Next.js app. Guarded by
**`ADMIN_API_KEY`**, which is *not* `API_KEY`: holding the read key must not confer the
ability to rewrite the weights, and the two need to rotate independently.

**This gate fails closed.** With `ADMIN_API_KEY` unset every endpoint here returns `503`.
An unset `API_KEY` only leaves a read-only score exposed, which is why *it* defaults to
open for local dev; an unset admin key would hand the engine to anyone who found the URL.

### The contract is schema-driven

`GET /settings` returns the values **and the schema to render them from**:

```jsonc
{
  "config":   { "thresholds": { "good": 65, … }, … },   // credentials removed entirely
  "defaults": { … },                                     // for a per-field "reset" affordance
  "sections": [ { "name": "thresholds", "title": "Thresholds", "description": "…" }, … ],
  "schema":   [ { "path": "thresholds.good", "type": "int", "min": 0, "max": 100,
                  "section": "thresholds", "label": "Good to trade ≥",
                  "help": "…", "note": "" }, … ],
  "meta":     { "secrets": {…}, "calendar": {…}, "json_sections": […] }
}
```

The panel switches on `type` (`int` `float` `bool` `str` `text` `time` `enum` `json`) and
groups by `section`. **Adding a setting is one entry in `app/service/settings_schema.py` and
no frontend release** — it appears in `schema` and the panel renders it. The same table
drives validation, so the bounds the UI shows are the bounds the API enforces.

| Endpoint | Does |
|---|---|
| `GET /settings` | Values + defaults + schema + secret/calendar status. |
| `PUT /settings` | `{"patch": {"thresholds": {"good": 70}}}` — send only what changed. |
| `POST /settings/reset` | `{"section": "weights"}`, or `{}` to reset everything. |
| `POST /secrets/{openai\|webz}` | Store a credential. Write-only. |
| `DELETE /secrets/{name}` | Remove the stored credential (an env var stays in force). |
| `POST /secrets/{name}/test` | Authenticate against the provider. Costs nothing. |
| `GET /actions` | Action catalogue + last outcome + scheduler state. |
| `POST /actions/{action}` | Start `predict`, `label`, `regrade`, `train`, `calendar-refresh`. |
| `GET /actions/jobs/{id}` | Poll a job: `status` is `running` \| `ok` \| `error`. |

### What a save reports back

Saving from a frontend never restarts the process, so `PUT` returns what actually happened
rather than leaving the panel to guess:

```jsonc
{ "ok": true, "changed": ["schedule.predict_time"],
  "effects": { "scheduler_restarted": true, "requires_regrade": false,
               "prediction_stale": false }, "warnings": [] }
```

- **`scheduler_restarted`** — the cron triggers were rebuilt, so a schedule change is live
  immediately. Without this it would sit in the DB looking saved while the old times kept
  firing until the next redeploy.
- **`requires_regrade`** — a label ER cutoff moved, so stored sessions are now graded on a
  mix of the old and new definitions. `POST /actions/regrade` to fix.
- **`prediction_stale`** — today's frozen verdict predates the change. It is *not* recut
  automatically; `POST /actions/predict` does that.

`changed` lists only paths whose value actually moved, so a no-op save says so.

### Validation

The Jinja page silently keeps the old value when a field won't parse. The API does the
opposite — a rejected patch writes **nothing** and comes back `422` with per-field messages:

```jsonc
{ "detail": { "message": "Validation failed",
              "errors": [ { "path": "thresholds.good",
                            "message": "must be above the caution line (40) …" } ] } }
```

Beyond types and bounds it enforces: unknown paths are **refused** (a typo that silently
writes `thresholds.gud` would look saved and do nothing); `good` must exceed `caution` and
the directional cutoff must exceed the choppy one; weights can't all be zero; the structural
sections (`session` `tickers` `gate` `ml`) must arrive **complete**, so a partial patch can't
drop `session.close`, and reject unknown keys and contradictions like a category listed as
both veto and warn.

### Credentials

Never returned — **removed** from `config`, not blanked, so a round-trip can't clear a
working key. Their state is in `meta.secrets` (`env_present`, `stored_present`, `source`,
`active`). The matching env var still wins at read time, so saving a key while
`OPENAI_API_KEY` is set returns a warning saying the stored one is inert.

### Actions run in the background

`predict` makes live yfinance + OpenAI calls (tens of seconds) and `train` runs for minutes —
longer than a Vercel route handler will wait. So `POST /actions/{action}` returns `202` with
a job id to poll (`200` instead, with the in-flight job, if that action is already running:
single-flight, so a double-click can't stack two predicts or have two trains fight over the
model file). Job records are **in-process memory** — a host restart loses them, and it
assumes one instance, the same assumption the scheduler already makes.

### Auth & hardening
- **`API_KEY`** guards `/api/v1/*`. Send `Authorization: Bearer <key>` or `X-API-Key: <key>`.
- **`ADMIN_API_KEY`** guards `/api/v1/admin/*`, same header. **Fails closed** (503 when
  unset). Startup logs a warning if it equals `API_KEY` or looks too short.
- **`ADMIN_USER` / `ADMIN_PASS`** guard the Jinja UI via HTTP Basic.
- **`FRONTEND_ORIGINS`** — comma-separated CORS allowlist (only needed for direct browser
  calls; server-side calls from Next.js don't need it, and are the recommended path since
  they keep both keys off the client).
- **Rate limiting** — per-IP caps (`RATELIMIT_ADMIN` default 30/60, `RATELIMIT_API` default
  240/60, `RATELIMIT_ADMIN_API` default 60/60); over the cap returns `429` with `Retry-After`.
  Tight on the admin surface (brute-force defence), generous on the product API (traffic
  shares the frontend server's IPs), in between on the admin API — it must not inherit the
  generous bucket just for living under `/api/`, but the panel polls running jobs.
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
3. Set env vars: `API_KEY`, `ADMIN_API_KEY`, `ADMIN_USER`, `ADMIN_PASS`, `FRONTEND_ORIGINS`,
   `TRADESCALE_DB=/data/tradescale.db`, and optionally `OPENAI_API_KEY`. (`.env.example` lists them.)
   Generate the two API keys separately — reusing one value defeats the read/write split.
4. First boot: `init_db` runs, the 161-day news cache seeds from the committed
   `data/news_seed.csv`, and the scheduler starts. The first `/today` returns
   `has_prediction=false` until predict fires (09:30 ET) or you POST `/run-predict` from the
   admin UI.
5. Point the Next.js frontend's server env at the Railway URL + `API_KEY`, and its admin
   panel at the same URL + `ADMIN_API_KEY`. Both belong in **server-only** env vars — never
   prefix either `NEXT_PUBLIC_`, and gate the admin route handlers behind your own admin check.

Render / Fly work the same way (any always-on container host with a persistent disk).

## Deploy caveats (by design, for the beta)
- **Single instance only.** The scheduler and the admin action jobs are in-process; 2+
  instances would double-fire the predict/label jobs, and a job started on one instance
  would be unpollable from the other. Fine for the beta — split the scheduler into its own
  worker before scaling.
- **SQLite needs the volume.** Without `TRADESCALE_DB` on a mounted disk, the DB is wiped on
  every redeploy. Migrating to Postgres is the natural next step once concurrency matters.
- **Free feeds are unofficial.** yfinance / ForexFactory are scrapers with no SLA and
  non-commercial terms — swap to licensed feeds before charging money. `GET /health` surfaces a
  silent scrape break.

## Not in this backend (frontend / later phases)
- User accounts, sessions, subscriptions (you build these with the dashboard).
- Stripe billing, "not financial advice" terms.
- Postgres, split scheduler worker.
