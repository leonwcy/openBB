"""Standalone ingestion for earnings-growth series used by valuation scoring."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

# Side effect: load config.env / .env
import ingest_daily_snapshot  # noqa: F401, E402

from database import engine_label, get_engines, init_db, session_scope  # noqa: E402
from earnings_growth_source import load_earnings_growth_rows  # noqa: E402
from macro_repo import upsert_macro_observation, upsert_macro_series_state  # noqa: E402

SERIES = ["SP500_EARNINGS_GROWTH", "NASDAQ_EARNINGS_GROWTH"]


def _years() -> int:
    import os

    return max(1, int(os.environ.get("LEON_MACRO_FULL_YEARS", "5")))


def _run():
    engines = init_db()
    end = date.today()
    start = end - timedelta(days=_years() * 366)
    total_rows = 0
    n_ok = 0

    for sid in SERIES:
        rows = load_earnings_growth_rows(sid, start, end)
        for eng in engines:
            with session_scope(eng) as session:
                for r in rows:
                    upsert_macro_observation(session, **r)
                upsert_macro_series_state(
                    session,
                    provider="alt",
                    series_id=sid,
                    last_observation_date=max((r["observation_date"] for r in rows), default=None),
                    success=True,
                )
                session.commit()
        total_rows += len(rows)
        if rows:
            n_ok += 1

    targets = " | ".join(engine_label(e) for e in engines)
    print(
        f"Earnings growth ingest done. Targets: {targets}. "
        f"series_with_data={n_ok}/{len(SERIES)}, rows={total_rows}, range={start}..{end}, "
        f"finished_at={datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}"
    )


if __name__ == "__main__":
    _run()

