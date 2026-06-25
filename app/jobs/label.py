"""Post-close job: label the latest session. Run via `python -m app.jobs.label`."""
from __future__ import annotations

from ..labeling.efficiency import run_labeling


def main() -> None:
    r = run_labeling()
    if r.get("error"):
        print(f"[label] {r['date']}  error: {r['error']}")
        return
    print(f"[label] {r['date']}  ER={r['realized_er']}  "
          f"label={r['realized_label']}  range%={r['range_pct']}  bars={r['bars']}")


if __name__ == "__main__":
    main()
