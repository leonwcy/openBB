"""
Backfill ~N years of daily OHLCV into daily_quotes (per symbol, OpenBB equity.price.historical).

Prereq: symbol_registry populated (e.g. run ingest_daily_snapshot first).


这个文件是跑所有的股票历史数据的

Env:
  LEON_EOD_BACKFILL_YEARS=5
  LEON_EOD_PROVIDER=fmp          # needs FMP_API_KEY for fmp
  LEON_BACKFILL_WORKERS=16       # thread pool size
  LEON_BACKFILL_SLEEP_SEC=0      # optional throttle per symbol
  LEON_BACKFILL_SERIAL_FETCH=0   # set 1 if OpenBB/FMP errors under concurrency
  LEON_BACKFILL_LIMIT=0          # 0 = all symbols in registry
  LEON_BACKFILL_EXCHANGES=
  LEON_BACKFILL_RESUME=1         # resume from progress file
  LEON_BACKFILL_PROGRESS_EVERY=1000
  LEON_BACKFILL_PROGRESS_FILE=e:/finance/OpenBB/leon/data/backfill_progress.json
  LEON_BACKFILL_CLEAR_PROGRESS_ON_COMPLETE=1

Uses same DATABASE_URL / DATABASE_MIRROR_URL as other scripts.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import threading

import pandas as pd
from sqlalchemy import select

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

# Side effect: load config.env / .env like daily ingest
import ingest_daily_snapshot  # noqa: F401, E402

from database import engine_label, get_engines, init_db, session_scope  # noqa: E402
from ingest_daily_snapshot import _get_fmp_api_key  # noqa: E402
from models import IngestionRun, SymbolRegistry  # noqa: E402
from quotes_repo import touch_registry, upsert_daily_quote  # noqa: E402

_FETCH_LOCK = threading.Lock()


def _years() -> int:
    return max(1, int(os.environ.get("LEON_EOD_BACKFILL_YEARS", "5")))


def _provider() -> str:
    return os.environ.get("LEON_EOD_PROVIDER", "fmp").strip().lower()


def _workers() -> int:
    return max(1, int(os.environ.get("LEON_BACKFILL_WORKERS", "16")))


def _sleep_sec(workers: int) -> float:
    raw = os.environ.get("LEON_BACKFILL_SLEEP_SEC")
    if raw is not None and raw.strip() != "":
        return float(raw)
    return 0.0 if workers > 1 else 0.05


def _serial_fetch() -> bool:
    return os.environ.get("LEON_BACKFILL_SERIAL_FETCH", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _fetch_context():
    return _FETCH_LOCK if _serial_fetch() else contextlib.nullcontext()


def _limit() -> int:
    v = int(os.environ.get("LEON_BACKFILL_LIMIT", "0"))
    return v if v > 0 else 0


def _resume_enabled() -> bool:
    return os.environ.get("LEON_BACKFILL_RESUME", "1").strip().lower() in ("1", "true", "yes")


def _progress_every() -> int:
    return max(1, int(os.environ.get("LEON_BACKFILL_PROGRESS_EVERY", "1000")))


def _progress_file() -> Path:
    raw = os.environ.get("LEON_BACKFILL_PROGRESS_FILE", "").strip()
    if raw:
        return Path(raw)
    return THIS_DIR / "backfill_progress.json"


def _clear_progress_on_complete() -> bool:
    return os.environ.get("LEON_BACKFILL_CLEAR_PROGRESS_ON_COMPLETE", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _symbol_key(symbol: str, exchange: str) -> str:
    return f"{exchange.upper()}|{symbol.upper()}"


def _scope_dict(
    provider: str,
    start: date,
    end: date,
    years: int,
    limit: int,
    exchanges: set[str] | None,
) -> dict:
    return {
        "provider": provider,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "years": years,
        "limit": limit,
        "exchanges": sorted(exchanges) if exchanges else [],
    }


def _read_progress(path: Path, scope: dict) -> tuple[set[str], int, int]:
    """Return (completed_keys, total_rows, n_sym_with_data) if scope matches else empty."""
    if not path.is_file():
        return set(), 0, 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set(), 0, 0
    if payload.get("scope") != scope:
        return set(), 0, 0
    completed = payload.get("completed_symbols", [])
    keys = {str(x) for x in completed}
    total_rows = int(payload.get("total_rows", 0) or 0)
    n_sym = int(payload.get("n_symbols_with_data", 0) or 0)
    return keys, total_rows, n_sym


def _save_progress(
    path: Path,
    *,
    scope: dict,
    total_scope: int,
    completed_keys: set[str],
    total_rows: int,
    n_sym_with_data: int,
    status: str,
) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "scope": scope,
        "status": status,
        "total_scope_symbols": total_scope,
        "completed_count": len(completed_keys),
        "completed_symbols": sorted(completed_keys),
        "total_rows": total_rows,
        "n_symbols_with_data": n_sym_with_data,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _exchange_filter() -> set[str] | None:
    raw = os.environ.get("LEON_BACKFILL_EXCHANGES", "").strip().lower()
    if not raw:
        return None
    return {x.strip() for x in raw.split(",") if x.strip()}


def _setup_obb(provider: str):
    from openbb import obb

    if provider == "fmp":
        key = _get_fmp_api_key()
        if not key:
            raise RuntimeError("FMP_API_KEY required when LEON_EOD_PROVIDER=fmp")
        obb.user.credentials.fmp_api_key = key
    return obb


def _norm_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    return out


def _historical_rows_for_symbol(
    obb, symbol: str, exchange: str, name: str | None, start: date, end: date, provider: str
) -> list[dict]:
    try:
        with _fetch_context():
            out = obb.equity.price.historical(
                symbol=symbol,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                interval="1d",
                provider=provider,
            )
            df = out.to_dataframe(index=None)
    except Exception:  # noqa: BLE001
        return []
    if df is None or df.empty:
        return []
    df = _norm_df(df)
    if "date" not in df.columns:
        return []
    df = df.sort_values("date").reset_index(drop=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows: list[dict] = []
    for j in range(len(df)):
        r = df.iloc[j]
        td = pd.to_datetime(r["date"], errors="coerce")
        if pd.isna(td):
            continue
        trade_date = td.date()
        vol = r.get("volume")
        try:
            vol_i = int(vol) if vol is not None and not (isinstance(vol, float) and pd.isna(vol)) else None
        except (TypeError, ValueError):
            vol_i = None
        prev_c = None
        chg = None
        if j > 0 and pd.notna(df.iloc[j - 1].get("close")) and pd.notna(r.get("close")):
            prev_c = float(df.iloc[j - 1]["close"])
            chg = (float(r["close"]) - prev_c) / prev_c * 100.0
        rows.append(
            {
                "symbol": symbol,
                "exchange": exchange,
                "trade_date": trade_date,
                "name": name,
                "open": float(r["open"]) if pd.notna(r.get("open")) else None,
                "high": float(r["high"]) if pd.notna(r.get("high")) else None,
                "low": float(r["low"]) if pd.notna(r.get("low")) else None,
                "close": float(r["close"]) if pd.notna(r.get("close")) else None,
                "volume": vol_i,
                "prev_close": prev_c,
                "change_percent": chg,
                "market_cap": None,
                "ma50": None,
                "ma200": None,
                "year_high": None,
                "year_low": None,
                "quote_timestamp": None,
                "ingested_at": now,
            }
        )
    return rows


def _load_symbols(session) -> tuple[list[tuple[str, str, str | None]], int]:
    ex_filter = _exchange_filter()
    q = select(SymbolRegistry.symbol, SymbolRegistry.exchange, SymbolRegistry.name).order_by(
        SymbolRegistry.exchange, SymbolRegistry.symbol
    )
    rows = session.execute(q).all()
    out: list[tuple[str, str, str | None]] = []
    for sym, ex, nm in rows:
        if ex_filter is not None and ex.lower() not in ex_filter:
            continue
        out.append((sym, ex, nm))
    total_after_filter = len(out)
    lim = _limit()
    if lim:
        return out[:lim], total_after_filter
    return out, total_after_filter


def _run():
    engines = init_db()
    provider = _provider()
    obb = _setup_obb(provider)
    years = _years()
    workers = _workers()
    sleep_s = _sleep_sec(workers)
    end = date.today()
    start = end - timedelta(days=int(years * 366))
    limit = _limit()
    ex_filter = _exchange_filter()
    scope = _scope_dict(provider, start, end, years, limit, ex_filter)
    progress_path = _progress_file()
    checkpoint_every = _progress_every()

    engine0 = engines[0]
    with session_scope(engine0) as session:
        symbols, total_after_filter = _load_symbols(session)
    if not symbols:
        raise RuntimeError("No symbols in symbol_registry (run ingest_daily_snapshot first).")
    if len(symbols) < total_after_filter:
        print(
            f"Backfill scope is LIMITED: processing {len(symbols)} / {total_after_filter} symbols "
            f"(LEON_BACKFILL_LIMIT={_limit()}). Set LEON_BACKFILL_LIMIT=0 for full run."
        )
    else:
        print(f"Backfill scope: processing all {len(symbols)} symbols.")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    run_refs: list[tuple[object, int]] = []
    for engine in engines:
        with session_scope(engine) as session:
            run_row = IngestionRun(
                started_at=now,
                finished_at=None,
                status="running",
                exchanges=f"eod_backfill:{provider}:{start}:{end}:workers={workers}",
                rows_inserted=0,
            )
            session.add(run_row)
            session.commit()
            run_refs.append((engine, run_row.id))

    completed_keys: set[str] = set()
    total_rows = 0
    n_sym = 0

    if _resume_enabled():
        loaded_keys, loaded_rows, loaded_n_sym = _read_progress(progress_path, scope)
        if loaded_keys:
            completed_keys = loaded_keys
            total_rows = loaded_rows
            n_sym = loaded_n_sym
            symbols = [s for s in symbols if _symbol_key(s[0], s[1]) not in completed_keys]
            print(
                f"Resume enabled: skipped {len(completed_keys)} completed symbols from "
                f"{progress_path}."
            )
    total_scope = len(symbols) + len(completed_keys)
    print(
        f"Progress checkpoint every {checkpoint_every} symbols -> {progress_path}"
    )

    def process_one(sym_tuple: tuple[str, str, str | None]) -> tuple[str, int, int]:
        symbol, exchange, name = sym_tuple
        key = _symbol_key(symbol, exchange)
        rows_local = _historical_rows_for_symbol(obb, symbol, exchange, name, start, end, provider)
        if not rows_local:
            if sleep_s > 0:
                time.sleep(sleep_s)
            return key, 0, 0
        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            for engine in engines:
                with session_scope(engine) as session:
                    for row in rows_local:
                        upsert_daily_quote(session, row)
                        touch_registry(session, row, utc_now)
                    session.commit()
        except Exception:
            traceback.print_exc()
            return key, 0, 0
        if sleep_s > 0:
            time.sleep(sleep_s)
        return key, len(rows_local), 1

    try:
        done = 0
        completed_since_save = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(process_one, t) for t in symbols]
            for fut in as_completed(futures):
                done += 1
                key, bar_n, sym_hit = fut.result()
                completed_keys.add(key)
                total_rows += bar_n
                n_sym += sym_hit
                completed_since_save += 1
                if done % 50 == 0:
                    print(
                        f"... {done}/{len(symbols)} tasks (resume total {len(completed_keys)}/{total_scope}), "
                        f"{total_rows} bar rows, {n_sym} symbols with data"
                    )
                if completed_since_save >= checkpoint_every:
                    _save_progress(
                        progress_path,
                        scope=scope,
                        total_scope=total_scope,
                        completed_keys=completed_keys,
                        total_rows=total_rows,
                        n_sym_with_data=n_sym,
                        status="running",
                    )
                    completed_since_save = 0

        finish = datetime.now(timezone.utc).replace(tzinfo=None)
        for engine, run_id in run_refs:
            with session_scope(engine) as session:
                run = session.get(IngestionRun, run_id)
                if run:
                    run.status = "ok"
                    run.finished_at = finish
                    run.rows_inserted = total_rows
                    session.commit()

        targets = " | ".join(engine_label(e) for e in engines)
        _save_progress(
            progress_path,
            scope=scope,
            total_scope=total_scope,
            completed_keys=completed_keys,
            total_rows=total_rows,
            n_sym_with_data=n_sym,
            status="completed",
        )
        if _clear_progress_on_complete() and progress_path.exists():
            progress_path.unlink()
        print(
            f"EOD backfill done. Targets: {targets}. workers={workers}. "
            f"Symbols with data: {n_sym}/{total_scope}, completed symbols: {len(completed_keys)}/{total_scope}, "
            f"bar rows upserted: {total_rows}, provider={provider}, range={start}..{end}"
        )
    except Exception as exc:  # noqa: BLE001
        err = f"{exc}\n{traceback.format_exc()}"
        finish = datetime.now(timezone.utc).replace(tzinfo=None)
        _save_progress(
            progress_path,
            scope=scope,
            total_scope=total_scope,
            completed_keys=completed_keys,
            total_rows=total_rows,
            n_sym_with_data=n_sym,
            status="error",
        )
        for engine, run_id in run_refs:
            with session_scope(engine) as session:
                run = session.get(IngestionRun, run_id)
                if run:
                    run.status = "error"
                    run.finished_at = finish
                    run.detail = err[:8000]
                    session.commit()
        raise


if __name__ == "__main__":
    _run()
