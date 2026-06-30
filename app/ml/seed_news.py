"""Portable news-cache seed: ship the GPT-scored history in git.

The expensive part of the news layer is the per-day GPT scoring (slow GDELT
throttling + paid API calls). Exporting the `news_scores` table to a
version-controlled CSV lets every clone / the Pi inherit it without re-running
(or even needing a key for) the backfill.

    python -m app.ml.seed_news export   # DB news_scores -> data/news_seed.csv (commit it)
    python -m app.ml.seed_news import    # data/news_seed.csv -> DB (idempotent; default)

On startup the app auto-imports the seed when its `news_scores` table is empty,
so a fresh pull on the Pi just works with the cached news already present. The
import never overwrites a row the local DB already has (ON CONFLICT DO NOTHING),
so it is safe to run against a live database.
"""
from __future__ import annotations

import csv
import sys

from ..db import DATA_DIR, get_conn, init_db

SEED_CSV = DATA_DIR / "news_seed.csv"
_COLS = ["date", "scored", "relevance", "expected_impact", "direction",
         "chop_risk", "rationale", "source", "headlines_json", "created_at"]


def export_seed(path=SEED_CSV) -> int:
    """Dump the news_scores table to a committable CSV. Returns row count."""
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT {', '.join(_COLS)} FROM news_scores ORDER BY date"
        ).fetchall()
    finally:
        conn.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_COLS)
        for r in rows:
            w.writerow([r[c] for c in _COLS])
    return len(rows)


def import_seed(path=SEED_CSV) -> int:
    """Load the seed CSV into news_scores idempotently. Returns rows inserted."""
    if not path.exists():
        return 0
    init_db()  # ensure the table exists
    conn = get_conn()
    n = 0
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                vals = [(row.get(c) if row.get(c) not in ("", None) else None)
                        for c in _COLS]
                cur = conn.execute(
                    f"INSERT INTO news_scores ({', '.join(_COLS)}) "
                    f"VALUES ({', '.join('?' for _ in _COLS)}) "
                    f"ON CONFLICT(date) DO NOTHING",
                    vals,
                )
                n += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return n


def seed_if_empty() -> int:
    """Auto-load the seed when the local cache is empty (called on startup)."""
    if not SEED_CSV.exists():
        return 0
    conn = get_conn()
    try:
        empty = conn.execute("SELECT COUNT(*) FROM news_scores").fetchone()[0] == 0
    except Exception:  # noqa: BLE001 - table not created yet
        empty = True
    finally:
        conn.close()
    return import_seed() if empty else 0


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "import"
    if mode == "export":
        n = export_seed()
        print(f"[seed] exported {n} news days -> {SEED_CSV}")
    else:
        n = import_seed()
        print(f"[seed] imported {n} new news days from {SEED_CSV}")


if __name__ == "__main__":
    main()
