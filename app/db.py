"""SQLite storage: connection, schema, and small helpers."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Repo-relative data dir holds the version-controlled assets (news seed, training
# set, trained model). It stays put so those paths never move.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# The live database can be redirected to a persistent volume in the cloud (the
# container filesystem is ephemeral) via TRADESCALE_DB, e.g. /data/tradescale.db.
# Unset -> the repo data dir, exactly as before on the dev box / Pi.
DB_PATH = Path(os.environ["TRADESCALE_DB"]) if os.environ.get("TRADESCALE_DB") else DATA_DIR / "tradescale.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS predictions (
    date              TEXT PRIMARY KEY,   -- YYYY-MM-DD (ET)
    created_at        TEXT NOT NULL,
    tier              TEXT NOT NULL,      -- VETO | WARN | CLEAN | CLOSED
    direction_quality INTEGER,           -- 0..100 (higher = more tradeable)
    chop_risk         REAL,              -- 0..1
    verdict           TEXT,
    reason            TEXT,
    warn_note         TEXT,
    features_json     TEXT
);

CREATE TABLE IF NOT EXISTS outcomes (
    date           TEXT PRIMARY KEY,      -- YYYY-MM-DD (ET)
    realized_er    REAL,                  -- session efficiency ratio 0..1
    realized_range REAL,                  -- session high-low
    range_pct      REAL,                  -- range as % of open
    realized_label TEXT,                  -- DIRECTIONAL | MIXED | CHOPPY
    bars           INTEGER,
    computed_at    TEXT,
    realized_net_pct REAL                 -- |close-open| as % of open (tradeability target)
);

CREATE TABLE IF NOT EXISTS model_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    n_samples    INTEGER,
    date_from    TEXT,
    date_to      TEXT,
    metrics_json TEXT,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS news_scores (
    date            TEXT PRIMARY KEY,      -- YYYY-MM-DD (ET)
    scored          INTEGER,               -- 1 if GPT scored it
    relevance       REAL,
    expected_impact REAL,
    direction       TEXT,                  -- risk_on | risk_off | uncertain
    chop_risk       REAL,
    rationale       TEXT,
    source          TEXT,
    headlines_json  TEXT,
    created_at      TEXT
);

-- The economic calendar, cached. ForexFactory's free feed is rate-limited and only
-- ever serves the CURRENT week, so we accumulate here instead of re-fetching per use:
--   * the gate reads this, so a 429/outage falls back to the last-known calendar
--     rather than an empty list (which used to silently grade an FOMC day CLEAN);
--   * /api/v1/calendar serves purely from here, so frontend polling costs 0 upstream
--     calls;
--   * next week's events land here as soon as FF rolls the feed over, which is what
--     makes the Friday week-ahead view possible at all.
-- Keyed by (date, country, title): a re-fetch UPDATES an event whose time or impact
-- was revised rather than duplicating it.
CREATE TABLE IF NOT EXISTS calendar_events (
    date       TEXT NOT NULL,          -- YYYY-MM-DD (ET)
    country    TEXT NOT NULL,
    title      TEXT NOT NULL,
    event_time TEXT,                   -- ISO8601 ET, or NULL for all-day/tentative
    impact     TEXT,                   -- High | Medium | Low | Holiday | ''
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (date, country, title)
);

CREATE INDEX IF NOT EXISTS idx_calendar_events_date ON calendar_events(date);

-- Single-row provenance for the calendar cache: when we last reached FF, and whether
-- it worked. Drives the `stale` flag the API and the dashboard surface.
CREATE TABLE IF NOT EXISTS calendar_fetch (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    fetched_at TEXT,                   -- last SUCCESSFUL fetch
    ok         INTEGER,                -- did the last attempt succeed
    error      TEXT,                   -- error from the last attempt, if it failed
    events     INTEGER,                -- events seen in the last successful fetch
    attempted_at TEXT                  -- last attempt, success or not
);

-- Fitted tradeability model (app/scoring/tradeability.py): both ridge legs, plus the
-- trailing reference distribution the 1-100 percentile is taken against. Append-only,
-- newest row wins - so a bad refit can be rolled back by deleting one row, and the
-- fit history stays auditable. The payload is small (a few hundred floats).
CREATE TABLE IF NOT EXISTS tradeability_model (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    n_samples    INTEGER,
    date_from    TEXT,
    date_to      TEXT,
    payload_json TEXT NOT NULL,
    notes        TEXT
);
"""


def get_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# Columns added to tables that already exist in deployed databases. CREATE TABLE IF
# NOT EXISTS silently leaves an existing table alone, so a new column has to be
# ALTERed in. Each entry is (table, column, definition) and is applied only when the
# column is genuinely absent, so this stays idempotent across restarts.
_MIGRATIONS = [
    ("outcomes", "realized_net_pct", "REAL"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, decl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)
        conn.commit()
    finally:
        conn.close()
