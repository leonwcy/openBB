"""
Daily job: pull US exchange-wide snapshots via OpenBB (FMP) and upsert into DB.

这个文件是跑所有的股票当天的数据

Usage:
  set FMP_API_KEY=...
  python ingest_daily_snapshot.py

Windows Task Scheduler: run run_daily.bat after activating your venv.

Requires: Python >= 3.10, packages in requirements.txt
"""

from __future__ import annotations

import json
import importlib
import os
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

# Prefer local OpenBB source tree over stale site-packages copy when this repo is present.
# This avoids extension/core API mismatch in scheduled runs.
OPENBB_CORE_SRC = THIS_DIR.parent.parent / "openbb_platform" / "core"
if OPENBB_CORE_SRC.is_dir() and str(OPENBB_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(OPENBB_CORE_SRC))


def _prioritize_local_openbb_source() -> None:
    """Force local openbb source to win import precedence over site-packages."""
    if not OPENBB_CORE_SRC.is_dir():
        return
    p = str(OPENBB_CORE_SRC)
    while p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)


def _bootstrap_credentials_env() -> None:
    """Load env files; use override=True so values win over empty shell vars."""
    for path in (
        THIS_DIR / "config.env",
        THIS_DIR / ".env",
        Path.cwd() / "config.env",
        Path.cwd() / ".env",
    ):
        if path.is_file():
            try:
                load_dotenv(path, override=True, encoding="utf-8-sig")
            except TypeError:
                load_dotenv(path, override=True)


_bootstrap_credentials_env()


def _parse_dotenv_line_for_key(line: str, key: str) -> str | None:
    """Parse KEY=value from one line (export KEY=value supported)."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if s.startswith("export "):
        s = s[7:].strip()
    if not s.startswith(f"{key}="):
        return None
    _, _, rest = s.partition("=")
    val = rest.strip().strip('"').strip("'")
    return val or None


def _read_api_key_from_env_file(path: Path, key: str = "FMP_API_KEY") -> str | None:
    """Fallback if python-dotenv misses the key (encoding, odd line endings, etc.)."""
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        v = _parse_dotenv_line_for_key(line, key)
        if v:
            return v.strip()
    return None


def _env_diagnostics() -> str:
    """Safe hints: file presence + whether an FMP_API_KEY assignment line exists."""
    parts = [f"cwd={Path.cwd()}", f"script_dir={THIS_DIR}"]
    candidates = (
        THIS_DIR / "config.env",
        THIS_DIR / ".env",
        Path.cwd() / "config.env",
        Path.cwd() / ".env",
    )
    for path in candidates:
        label = str(path)
        if not path.is_file():
            parts.append(f"{label}: missing")
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            parts.append(f"{label}: unreadable ({exc})")
            continue
        has_assign = any(
            _parse_dotenv_line_for_key(line, "FMP_API_KEY") is not None
            for line in text.splitlines()
        )
        parts.append(f"{label}: exists size={path.stat().st_size} has_FMP_line={has_assign}")
    obb_path = Path.home() / ".openbb_platform" / "user_settings.json"
    parts.append(f"openbb_user_settings: exists={obb_path.is_file()} path={obb_path}")
    return "\n".join(parts)

from database import engine_label, get_engines, init_db, session_scope  # noqa: E402
from models import IngestionRun  # noqa: E402
from quotes_repo import touch_registry, upsert_daily_quote  # noqa: E402


def _us_exchanges() -> list[str]:
    raw = os.environ.get("LEON_US_EXCHANGES", "nyse,nasdaq,amex")
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _fmp_key_from_openbb_user_settings() -> str | None:
    path = Path.home() / ".openbb_platform" / "user_settings.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        creds = data.get("credentials") or {}
        v = creds.get("fmp_api_key")
        if isinstance(v, str) and v.strip() and v.strip().upper() != "REPLACE_ME":
            return v.strip()
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        pass
    return None


def _get_fmp_api_key() -> str | None:
    for name in ("FMP_API_KEY", "OPENBB_FMP_API_KEY"):
        raw = os.environ.get(name)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    for path in (
        THIS_DIR / "config.env",
        THIS_DIR / ".env",
        Path.cwd() / "config.env",
        Path.cwd() / ".env",
    ):
        manual = _read_api_key_from_env_file(path, "FMP_API_KEY")
        if manual:
            return manual
        manual_alt = _read_api_key_from_env_file(path, "OPENBB_FMP_API_KEY")
        if manual_alt:
            return manual_alt
    return _fmp_key_from_openbb_user_settings()


def _setup_openbb_credentials():
    _prioritize_local_openbb_source()
    import openbb as openbb_pkg

    def _has_accessor(obj, name: str) -> bool:
        try:
            getattr(obj, name)
            return True
        except Exception:  # noqa: BLE001
            return False

    # Task Scheduler may load an environment where extension package import fails.
    # Try rebuilding/reloading once so `obb.equity` exists.
    obb = openbb_pkg.obb
    if not _has_accessor(obb, "equity"):
        try:
            openbb_pkg.build(lint=False, verbose=False)
            openbb_pkg = importlib.reload(openbb_pkg)
            obb = openbb_pkg.obb
        except Exception:  # noqa: BLE001
            pass

    key = _get_fmp_api_key()
    if not key:
        cfg = THIS_DIR / "config.env"
        hint = (
            "No FMP API key found.\n"
            f"- Put FMP_API_KEY=... in {cfg} (copy from config.example.env), not only in config.example.env.\n"
            "- Or set env var FMP_API_KEY in the shell / Task Scheduler.\n"
            "- Or add credentials.fmp_api_key to ~/.openbb_platform/user_settings.json\n"
            "- Use ASCII KEY=value format; avoid smart quotes. Example: FMP_API_KEY=abc123\n"
            "\nDiagnostics:\n"
            + _env_diagnostics()
        )
        raise RuntimeError(hint)
    if not _has_accessor(obb, "equity"):
        raise RuntimeError(
            "OpenBB extensions are not loaded in this runtime (missing `obb.equity`). "
            "Run using the same venv as local success, and ensure openbb extensions are installed."
        )
    obb.user.credentials.fmp_api_key = key
    return obb


def _normalize_percent(pct: float | None) -> float | None:
    if pct is None:
        return None
    if abs(pct) <= 1 and pct != 0:
        return pct * 100.0
    return pct


def _quote_row_from_result(r, exchange: str) -> dict:
    d = r.model_dump() if hasattr(r, "model_dump") else dict(r)
    sym = d.get("symbol")
    if not sym:
        return {}
    ts = d.get("last_price_timestamp")
    if isinstance(ts, datetime):
        td = ts.date()
    elif isinstance(ts, date):
        td = ts
    else:
        td = date.today()
    mc = d.get("market_cap")
    return {
        "symbol": sym,
        "exchange": exchange,
        "trade_date": td,
        "name": d.get("name"),
        "open": d.get("open"),
        "high": d.get("high"),
        "low": d.get("low"),
        "close": d.get("close"),
        "volume": d.get("volume"),
        "prev_close": d.get("prev_close"),
        "change_percent": _normalize_percent(d.get("change_percent")),
        "market_cap": float(mc) if mc is not None else None,
        "ma50": d.get("ma50"),
        "ma200": d.get("ma200"),
        "year_high": d.get("year_high"),
        "year_low": d.get("year_low"),
        "quote_timestamp": ts if isinstance(ts, datetime) else None,
        "ingested_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }


def _run():
    engines = init_db()
    obb = _setup_openbb_credentials()
    exchanges = _us_exchanges()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    run_refs: list[tuple[object, int]] = []
    for engine in engines:
        with session_scope(engine) as session:
            run_row = IngestionRun(
                started_at=now,
                finished_at=None,
                status="running",
                exchanges=",".join(exchanges),
                rows_inserted=0,
            )
            session.add(run_row)
            session.commit()
            run_refs.append((engine, run_row.id))

    total_upserts = 0
    try:
        for ex in exchanges:
            out = obb.equity.market_snapshots(provider="fmp", market=ex)
            if not out.results:
                continue
            rows = []
            for r in out.results:
                row = _quote_row_from_result(r, ex)
                if row:
                    rows.append(row)

            utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
            batch_n = len(rows)
            for engine in engines:
                with session_scope(engine) as session:
                    for row in rows:
                        upsert_daily_quote(session, row)
                        touch_registry(session, row, utc_now)
                    session.commit()
            total_upserts += batch_n

        finish = datetime.now(timezone.utc).replace(tzinfo=None)
        for engine, run_id in run_refs:
            with session_scope(engine) as session:
                run = session.get(IngestionRun, run_id)
                if run:
                    run.status = "ok"
                    run.finished_at = finish
                    run.rows_inserted = total_upserts
                    session.commit()

        targets = " | ".join(engine_label(e) for e in engines)
        print(
            f"Done. Targets: {targets}. "
            f"Upserted {total_upserts} daily quote rows across {len(exchanges)} exchanges."
        )

    except Exception as exc:  # noqa: BLE001
        err = f"{exc}\n{traceback.format_exc()}"
        finish = datetime.now(timezone.utc).replace(tzinfo=None)
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
