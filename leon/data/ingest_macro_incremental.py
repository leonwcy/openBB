"""Incremental macro ingest with fallback full-window bootstrap."""

from __future__ import annotations

import json
import importlib
import os
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import select

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

# Side effect: load config.env / .env
import ingest_daily_snapshot  # noqa: F401, E402

from database import engine_label, get_engines, init_db, session_scope  # noqa: E402
from alt_pizza_source import load_alt_series_rows  # noqa: E402
from earnings_growth_source import load_earnings_growth_rows  # noqa: E402
from macro_repo import upsert_macro_observation, upsert_macro_series_state  # noqa: E402
from macro_series_seed import seed_macro_catalog  # noqa: E402
from models import IngestionRun, MacroSeriesCatalog, MacroSeriesState  # noqa: E402


def _workers() -> int:
    return max(1, int(os.environ.get("LEON_MACRO_WORKERS", "8")))


def _max_priority() -> int:
    return max(1, int(os.environ.get("LEON_MACRO_MAX_PRIORITY", "3")))


def _buffer_days() -> int:
    return max(1, int(os.environ.get("LEON_MACRO_INCREMENTAL_BUFFER_DAYS", "14")))


def _bootstrap_years() -> int:
    return max(1, int(os.environ.get("LEON_MACRO_BOOTSTRAP_YEARS", "5")))


def _get_fred_api_key() -> str | None:
    for name in ("FRED_API_KEY", "fred_api_key"):
        raw = os.environ.get(name)
        if raw is not None and raw.strip():
            return raw.strip()
    path = Path.home() / ".openbb_platform" / "user_settings.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        creds = data.get("credentials") or {}
        v = creds.get("fred_api_key")
        if isinstance(v, str) and v.strip() and v.strip().upper() != "REPLACE_ME":
            return v.strip()
    except Exception:  # noqa: BLE001
        return None
    return None


def _setup_obb():
    ingest_daily_snapshot._prioritize_local_openbb_source()
    import openbb as openbb_pkg

    def _has_accessor(obj, name: str) -> bool:
        try:
            getattr(obj, name)
            return True
        except Exception:  # noqa: BLE001
            return False

    obb = openbb_pkg.obb
    if not _has_accessor(obb, "economy"):
        try:
            openbb_pkg.build(lint=False, verbose=False)
            openbb_pkg = importlib.reload(openbb_pkg)
            obb = openbb_pkg.obb
        except Exception:  # noqa: BLE001
            pass

    key = _get_fred_api_key()
    if key:
        obb.user.credentials.fred_api_key = key
    if not _has_accessor(obb, "economy"):
        raise RuntimeError(
            "OpenBB extensions are not loaded in this runtime (missing `obb.economy`). "
            "Run using the same venv as local success, and ensure openbb extensions are installed."
        )
    return obb


def _load_catalog_with_state(session, max_priority: int) -> list[tuple[MacroSeriesCatalog, MacroSeriesState | None]]:
    rows = session.execute(
        select(MacroSeriesCatalog)
        .where(
            MacroSeriesCatalog.is_active.is_(True),
            MacroSeriesCatalog.priority_tier <= max_priority,
        )
        .order_by(MacroSeriesCatalog.priority_tier, MacroSeriesCatalog.provider, MacroSeriesCatalog.series_id)
    ).scalars().all()
    out: list[tuple[MacroSeriesCatalog, MacroSeriesState | None]] = []
    for c in rows:
        st = session.execute(
            select(MacroSeriesState).where(
                MacroSeriesState.provider == c.provider,
                MacroSeriesState.series_id == c.series_id,
            )
        ).scalar_one_or_none()
        out.append((c, st))
    return out


def _extract_series_rows(obb, item: MacroSeriesCatalog, start_date: date, end_date: date) -> list[dict]:
    if item.provider == "alt":
        if item.series_id in {"SP500_EARNINGS_GROWTH", "NASDAQ_EARNINGS_GROWTH"}:
            return load_earnings_growth_rows(item.series_id, start_date, end_date)
        return load_alt_series_rows(item.series_id, start_date, end_date)
    if item.provider != "fred":
        return []
    try:
        out = obb.economy.fred_series(
            symbol=item.series_id,
            provider="fred",
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )
        df = out.to_dataframe(index=None)
    except Exception:  # noqa: BLE001
        return []
    if df is None or df.empty:
        return []
    df.columns = [str(c) for c in df.columns]
    cols_lower_map = {c.lower(): c for c in df.columns}
    date_col = cols_lower_map.get("date")
    value_col = cols_lower_map.get(item.series_id.lower())
    if value_col is None:
        non_date = [c for c in df.columns if c.lower() != "date"]
        if not non_date:
            return []
        value_col = non_date[0]
    if date_col is None:
        return []

    out_rows: list[dict] = []
    for _, rec in df.iterrows():
        d = pd.to_datetime(rec[date_col], errors="coerce")
        if pd.isna(d):
            continue
        raw = rec[value_col]
        val: float | None = None
        txt: str | None = None
        try:
            if pd.notna(raw):
                val = float(raw)
        except Exception:  # noqa: BLE001
            txt = str(raw) if pd.notna(raw) else None
        out_rows.append(
            {
                "provider": item.provider,
                "series_id": item.series_id,
                "observation_date": d.date(),
                "value": val,
                "value_text": txt,
                "vintage_date": date.today(),
                "released_at": None,
            }
        )
    return out_rows


def _run():
    seed_macro_catalog()
    engines = init_db()
    obb = _setup_obb()
    workers = _workers()
    max_priority = _max_priority()
    buffer_days = _buffer_days()
    bootstrap_years = _bootstrap_years()
    today = date.today()

    with session_scope(engines[0]) as session:
        catalog_state = _load_catalog_with_state(session, max_priority)
    if not catalog_state:
        raise RuntimeError("macro_series_catalog is empty.")

    run_refs: list[tuple[object, int]] = []
    run_started = datetime.now(timezone.utc).replace(tzinfo=None)
    for eng in engines:
        with session_scope(eng) as session:
            row = IngestionRun(
                started_at=run_started,
                status="running",
                exchanges=(
                    "macro_incremental:"
                    f"priority<={max_priority}:buffer={buffer_days}:bootstrap_years={bootstrap_years}"
                ),
                rows_inserted=0,
            )
            session.add(row)
            session.commit()
            run_refs.append((eng, row.id))

    total_rows = 0
    n_series_data = 0
    try:
        tasks: list[tuple[MacroSeriesCatalog, date, date]] = []
        for item, st in catalog_state:
            if st and st.last_observation_date:
                start = st.last_observation_date - timedelta(days=buffer_days)
            else:
                start = today - timedelta(days=int(bootstrap_years * 366))
            tasks.append((item, start, today))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_extract_series_rows, obb, item, start, end): (item, start, end)
                for item, start, end in tasks
            }
            done = 0
            for fut in as_completed(futures):
                item, start, end = futures[fut]
                done += 1
                rows = fut.result()
                for eng in engines:
                    with session_scope(eng) as session:
                        for r in rows:
                            upsert_macro_observation(session, **r)
                        upsert_macro_series_state(
                            session,
                            provider=item.provider,
                            series_id=item.series_id,
                            last_observation_date=max((r["observation_date"] for r in rows), default=None),
                            success=True,
                        )
                        session.commit()
                total_rows += len(rows)
                if rows:
                    n_series_data += 1
                if done % 10 == 0:
                    print(f"... {done}/{len(tasks)} series, upserted={total_rows}")

        finish = datetime.now(timezone.utc).replace(tzinfo=None)
        for eng, run_id in run_refs:
            with session_scope(eng) as session:
                run = session.get(IngestionRun, run_id)
                if run:
                    run.status = "ok"
                    run.finished_at = finish
                    run.rows_inserted = total_rows
                    session.commit()
        targets = " | ".join(engine_label(e) for e in engines)
        print(
            f"Macro INCREMENTAL done. Targets: {targets}. series_with_data={n_series_data}/{len(tasks)}, "
            f"rows={total_rows}"
        )
    except Exception as exc:  # noqa: BLE001
        err = f"{exc}\n{traceback.format_exc()}"
        finish = datetime.now(timezone.utc).replace(tzinfo=None)
        for eng, run_id in run_refs:
            with session_scope(eng) as session:
                run = session.get(IngestionRun, run_id)
                if run:
                    run.status = "error"
                    run.finished_at = finish
                    run.detail = err[:8000]
                    session.commit()
        raise


if __name__ == "__main__":
    _run()

