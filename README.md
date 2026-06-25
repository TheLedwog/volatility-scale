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

## Data sources (Phase 1, all free)

- **Prices / VIX / futures:** yfinance.
- **Economic calendar:** ForexFactory's free weekly JSON feed (the same data you
  already use). Behind a `CalendarProvider` interface, so a paid feed is a drop-in.
- **News / LLM:** interfaces are stubbed (Phase 4). Add your OpenAI key in Settings
  when we wire up news scoring.

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
