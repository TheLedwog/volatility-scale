"""The calendar cache: the only thing that reads/writes `calendar_events`.

Why a cache at all - ForexFactory's free weekly JSON is rate-limited (a handful of
rapid requests earns a 429 that lasts minutes) and serves only the CURRENT week.
Fetching it per use was therefore both fragile and limiting:

  * The gate fetched it at predict time. A 429 raised, the engine swallowed it and
    scored the day on an EMPTY event list - which is indistinguishable from "nothing
    scheduled", so a rate-limited FOMC morning graded CLEAN / "good to trade". The
    gate now reads this table, so a failed fetch falls back to the last-known
    calendar and the day keeps its VETO.
  * A frontend-polled /calendar endpoint hitting FF per request would have made that
    far more likely. Reads here are free.
  * Next week's events only exist upstream once FF rolls the feed over (some time on
    Friday night / the weekend). Accumulating every event we ever see means the
    week-ahead view is simply "what do we know about dates after today".
"""
from __future__ import annotations

from datetime import date, datetime

from .db import get_conn, init_db
from .timeutils import ET, now_et


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).astimezone(ET)
    except (ValueError, TypeError):
        return None


def _row_to_event(row) -> dict:
    """A cache row in the shape the gate expects (see providers.base.CalendarEvent)."""
    return {
        "title": row["title"],
        "country": row["country"],
        "time": _parse_time(row["event_time"]),
        "impact": row["impact"] or "",
    }


def upsert_events(events: list[dict], fetched_at: datetime | None = None) -> int:
    """Store a batch of freshly-fetched events. Returns how many rows were written.

    Upserts on (date, country, title) so FF revising an event's time or impact updates
    the row in place. Events are never deleted: an event FF drops from the feed (or that
    scrolls out of the current week) stays in the cache, which is what lets the gate
    survive an outage and the week-ahead view outlive the feed's one-week window.
    """
    if not events:
        return 0
    stamp = (fetched_at or now_et()).isoformat(timespec="seconds")

    rows = []
    for e in events:
        t = e.get("time")
        d = t.date() if t else e.get("date")
        if not d:
            continue  # undateable event - nothing to key it on
        rows.append((
            d.isoformat() if isinstance(d, date) else str(d),
            e.get("country", ""),
            e.get("title", ""),
            t.isoformat() if t else None,
            e.get("impact", "") or "",
            stamp,
        ))

    conn = get_conn()
    try:
        conn.executemany(
            """
            INSERT INTO calendar_events (date, country, title, event_time, impact, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, country, title) DO UPDATE SET
                event_time=excluded.event_time,
                impact=excluded.impact,
                fetched_at=excluded.fetched_at
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def events_for(d: date, country: str = "USD") -> list[dict]:
    """Cached events for one date, as the gate wants them. Empty if we know of none."""
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM calendar_events WHERE date=? AND country=?",
            (d.isoformat(), country),
        ).fetchall()
        return [_row_to_event(r) for r in rows]
    finally:
        conn.close()


def events_between(start: date, end: date, country: str = "USD") -> dict[str, list[dict]]:
    """Cached events for an inclusive date range, grouped by ISO date string."""
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM calendar_events WHERE date BETWEEN ? AND ? AND country=? "
            "ORDER BY date, event_time",
            (start.isoformat(), end.isoformat(), country),
        ).fetchall()
    finally:
        conn.close()

    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["date"], []).append(_row_to_event(r))
    return grouped


def has_events_for(d: date, country: str = "USD") -> bool:
    """Do we hold ANY cached event for this date?

    Distinguishes "we fetched and the day is genuinely empty" from "we never got the
    calendar" only in combination with `last_fetch()` - see `service.calendar.day_events`.
    """
    init_db()
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM calendar_events WHERE date=? AND country=? LIMIT 1",
            (d.isoformat(), country),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_fetch(ok: bool, error: str | None = None, events: int = 0) -> None:
    """Record a fetch attempt. `fetched_at` only advances on success."""
    stamp = now_et().isoformat(timespec="seconds")
    conn = get_conn()
    try:
        if ok:
            conn.execute(
                """
                INSERT INTO calendar_fetch (id, fetched_at, ok, error, events, attempted_at)
                VALUES (1, ?, 1, NULL, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    fetched_at=excluded.fetched_at, ok=1, error=NULL,
                    events=excluded.events, attempted_at=excluded.attempted_at
                """,
                (stamp, events, stamp),
            )
        else:
            # Keep the last SUCCESSFUL fetched_at - it's what staleness is measured from.
            conn.execute(
                """
                INSERT INTO calendar_fetch (id, fetched_at, ok, error, events, attempted_at)
                VALUES (1, NULL, 0, ?, 0, ?)
                ON CONFLICT(id) DO UPDATE SET
                    ok=0, error=excluded.error, attempted_at=excluded.attempted_at
                """,
                (error or "unknown error", stamp),
            )
        conn.commit()
    finally:
        conn.close()


def last_fetch() -> dict:
    """Provenance of the cache: {fetched_at, ok, error, events, attempted_at}."""
    init_db()
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM calendar_fetch WHERE id=1").fetchone()
    finally:
        conn.close()
    if not row:
        return {"fetched_at": None, "ok": None, "error": None,
                "events": 0, "attempted_at": None}
    return dict(row)


def age_hours() -> float | None:
    """Hours since the last SUCCESSFUL fetch, or None if we've never had one."""
    fetched_at = last_fetch().get("fetched_at")
    if not fetched_at:
        return None
    try:
        then = datetime.fromisoformat(fetched_at)
    except (ValueError, TypeError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=ET)
    return (now_et() - then).total_seconds() / 3600.0
