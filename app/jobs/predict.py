"""Pre-open job: build today's prediction. Run via `python -m app.jobs.predict`."""
from __future__ import annotations

from ..scoring.engine import run_prediction


def main() -> None:
    r = run_prediction()
    dq = r.get("direction_quality")
    print(f"[predict] {r['date']}  tier={r['tier']}  "
          f"direction_quality={dq}  verdict={r['verdict']}")
    if r.get("reason"):
        print(f"          reason: {r['reason']}")
    if r.get("warn_note"):
        print(f"          warn:   {r['warn_note']}")
    if r.get("calendar_error"):
        print(f"          (calendar unavailable: {r['calendar_error']})")


if __name__ == "__main__":
    main()
