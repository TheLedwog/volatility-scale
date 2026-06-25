"""SQLite storage: connection, schema, and small helpers."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "tradescale.db"

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
"""


def get_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
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
