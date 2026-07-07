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
    computed_at    TEXT
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
"""


def get_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
