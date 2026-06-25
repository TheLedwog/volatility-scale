# Trade / Don't-Trade Scale (Phase 1)

A local tool that, before the New York open, gives a NY-session verdict for the
S&P 500 / NASDAQ in three tiers:

- **VETO** — a market-moving event (FOMC/rates, or a big 10:00 ET release) is
  scheduled *during* the session → don't trade.
- **WARN** — an 8:30 ET giant (CPI, NFP, …) lands before the open → caution, your call.
- **Score** — an arrow on a bar rating how likely the day is to **trend (trade)**
  vs **chop (don't)**.

It logs every prediction and, after the close, measures what actually happened
(session Efficiency Ratio) so the tool can be calibrated and, in later phases,
learn. See `PLAN.md` for the full design and roadmap.

> Phase 1 is the transparent **rule-based** engine. No ML yet — that's Phase 3.
> The numbers are interpretable placeholders meant to be tuned and later replaced
> by a trained model.

## Quick start

```bash
# 1. create a virtual environment
python -m venv .venv

# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux / Raspberry Pi:
# source .venv/bin/activate

# 2. install dependencies
pip install -r requirements.txt

# 3. run the app
python run.py
```

Then open <http://localhost:8000>.

- **Dashboard** (`/`) — today's verdict + gauge + factor breakdown + events.
  Click **Run prediction now** to (re)compute.
- **History** (`/history`) — past predictions vs realized outcomes + accuracy.
- **Settings** (`/settings`) — edit factor weights, thresholds, the veto/warn
  event lists, tickers, schedule, and API keys (all stored in the DB, no redeploy).

## Running the jobs manually

```bash
python -m app.jobs.predict     # build today's prediction
python -m app.jobs.label       # label the most recent completed session
```

The built-in scheduler (APScheduler) runs these automatically at the times set in
Settings (default 08:45 ET predict, 16:20 ET label) while `run.py` is running.
On a Raspberry Pi you can instead disable the scheduler and use cron / systemd
timers to call the two job commands above.

## Training the model (Phase 3)

The soft score can come from either the transparent **rule-based** engine or a
**trained model** (scikit-learn `HistGradientBoostingRegressor`). Build/refresh it:

```bash
python -m app.ml.build        # download ~2y of data, build dataset, train
# or the steps separately:
python -m app.ml.dataset      # -> data/training.csv
python -m app.ml.train        # -> data/model.joblib + metrics
```

…or click **Retrain model** on the History page. Metrics (rank correlation, and
realized ER of the model's top vs bottom third on a chronological hold-out) show on
that page.

`scoring.mode` (Settings) controls which engine runs:

- `auto` (default) — use the model **only if it beats near-random** on its hold-out
  (`ml.min_spearman` / `ml.min_lift`); otherwise fall back to the rules.
- `rules` — always the rule-based engine.
- `model` — always the model (even if weak).

> **Honest note:** with only price/vol/seasonal features and ~2 years of data, the
> bootstrap model currently has little edge over the rules — so `auto` keeps using
> the rules until a retrain (more data, plus the Phase 4 calendar/news features)
> earns it. Also, the bootstrap label uses **hourly** bars while the live labeler
> uses **5-min** bars, so their Efficiency-Ratio values aren't on the same scale.

## News & geopolitics (Phase 4)

Free headlines from **GDELT** (no key) read by your **OpenAI/GPT** key. GPT judges
how today's news is likely to affect the NY session and returns a `chop_risk`
(higher when news is high-impact but conflicting/uncertain), shown as a dashboard
panel and folded into the rule-based score as an optional `news_risk` factor.

To turn on the GPT read:

1. Settings → `providers` → set `openai_api_key` (and optionally `openai_model`,
   default `gpt-4o-mini`). Keep `news.enabled` = `true`.
2. Re-run a prediction. The dashboard "News & geopolitics" panel will show GPT's
   read, and `news_risk` joins the factor breakdown.

Without a key you still see the headlines (just no GPT read, and the factor is
skipped). Notes:

- One GPT call per day, cached — re-running won't re-bill. GDELT is rate-limited,
  so headlines are cached per day too.
- This feeds the **rule-based** engine, not the trained model yet. Making news a
  *model* feature needs a historical GPT-scored backfill (a cheap but separate
  opt-in step, since it scores ~2y of past days).
- `news.query` (Settings) controls what GDELT searches for.

## Data sources

- **Prices / VIX / futures:** yfinance (free).
- **Economic calendar:** ForexFactory's free weekly JSON feed (the same data you
  already use). Behind a `CalendarProvider` interface, so a paid feed is a drop-in.
- **News / geopolitics:** GDELT (free, no key), scored by your OpenAI/GPT key.
  Behind `NewsProvider` / `LLMProvider` interfaces — both swappable.

## Choosing the port

The app listens on **8000** by default and binds `0.0.0.0` (reachable from other
machines on your LAN). It does **not** auto-pick a free port — if 8000 is already
taken by another service on your Pi, set a different one:

```bash
PORT=8001 python run.py
```

## Deploy on a Raspberry Pi / Linux server

Requires Python 3.10+ (Raspberry Pi OS Bookworm ships 3.11 — fine).

```bash
# 1. system packages
sudo apt update && sudo apt install -y python3-venv python3-pip git

# 2. clone (use your repo URL)
git clone https://github.com/<you>/volatility-scale.git
cd volatility-scale

# 3. venv + deps  (first install on a Pi can take a few minutes)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 4. test it (pick a free port)
PORT=8001 python run.py        # then visit http://<pi-ip>:8001
```

### Run it as a background service (starts on boot)

```bash
# edit deploy/tradescale.service first: set User, WorkingDirectory and PORT
sudo cp deploy/tradescale.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradescale

sudo systemctl status tradescale     # check it's running
journalctl -u tradescale -f          # follow logs
```

The built-in scheduler runs the predict/label jobs automatically. If you'd rather
use cron, set `schedule.enabled` to `false` in Settings and add:

```cron
45 8  * * 1-5  cd /home/pi/volatility-scale && .venv/bin/python -m app.jobs.predict
20 16 * * 1-5  cd /home/pi/volatility-scale && .venv/bin/python -m app.jobs.label
```

> cron uses the Pi's local timezone — set the Pi to `America/New_York`
> (`sudo timedatectl set-timezone America/New_York`) or adjust the cron times.

## Notes

- Everything is anchored to `America/New_York`; `tzdata` is installed so timezones
  work correctly on Windows too.
- Needs internet access at run time to fetch prices and the calendar. If a fetch
  fails, the affected factor degrades to neutral instead of crashing.
- This is decision support, **not** financial advice.
