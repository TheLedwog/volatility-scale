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

### Your OpenAI key (read this)

Provide the key the **secure way** — as an environment variable, not in the UI:

```bash
# a git-ignored .env file in the project root (loaded automatically)
echo 'OPENAI_API_KEY=sk-...' > .env
chmod 600 .env          # Linux/Pi: restrict to your user
```

…or export `OPENAI_API_KEY` / set it in the systemd unit (`Environment=` /
`EnvironmentFile=`). The key is read from the environment first and **never written
to the database or shown in the UI**.

You *can* instead paste it into Settings → `providers` → `openai_api_key`, but then
it's stored in the local DB. The UI masks it (`********`) and never echoes it back,
but the env-var route is preferred. Note the web app has no login and listens on
your LAN — keep it on a trusted network (or bind to localhost and use an SSH tunnel).

Then keep `news.enabled = true` and re-run a prediction: the "News & geopolitics"
panel fills with GPT's read and `news_risk` joins the factor breakdown. Without a
key you still get headlines (no GPT read; the factor is skipped).

- One GPT call per day, cached — re-running won't re-bill. GDELT is rate-limited so
  headlines are cached per day too. `news.query` controls the GDELT search.

### Adding news into the *model* (optional backfill)

The live news layer feeds the **rule-based** engine immediately. To let the
**trained model** learn from news, score ~2 years of past days once and retrain:

```bash
python -m app.ml.backfill_news          # DRY RUN: prints a cost estimate, spends nothing
python -m app.ml.backfill_news --yes    # actually run it (cached + resumable)
```

On `gpt-4o-mini` this is roughly **$0.15–0.25 one-time** for ~720 sessions, and it
adds `news_*` features to the model, then retrains. It's safe by default (won't
spend without `--yes`) and resumable (re-runs skip already-scored days). Takes
~20–40 min due to polite GDELT throttling. As always, the model only auto-activates
if it then beats the rules.

### Carrying the news data to your Pi / other machines

The backfill is slow and costs money, so its result is shipped **in the repo** and
travels with every clone — you never re-run it elsewhere. Two files are committed
(everything else under `data/` stays git-ignored, including the live `tradescale.db`,
so a pull never clobbers a machine's own prediction history):

- `data/news_seed.csv` — the GPT-scored news cache (the expensive part).
- `data/training.csv` — the training set with the `news_*` columns baked in.

On startup the app **auto-imports** `news_seed.csv` into any database whose news
cache is empty (it never overwrites rows already there). So upgrading the Pi is
just a pull + a retrain that rebuilds the *identical* model locally (the model is
deterministic — same data in, same model out — so the binary isn't shipped):

```bash
cd ~/volatility-scale
git pull
.venv/bin/python -m app.ml.train        # rebuild model.joblib from the shipped training.csv
sudo systemctl restart tradescale       # if running as a service (auto-imports the news cache)
```

`python -m app.ml.train` reads the committed `training.csv` (news already in it), so
**no OpenAI key or internet is needed on the Pi** to get the news-aware model. To
re-export the seed after scoring more days locally: `python -m app.ml.seed_news export`,
then commit `data/news_seed.csv` (and the refreshed `data/training.csv`).

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
