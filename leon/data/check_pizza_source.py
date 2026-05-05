"""Preflight check for Pentagon Pizza Index API mapping.

Validates:
1) URL connectivity
2) JSON shape and root extraction
3) Required fields (date/value, optional series filter)
4) Parseability and sample rows
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from json import JSONDecodeError
from urllib.request import urlopen

import pandas as pd

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

# Side effect: load config.env / .env
import ingest_daily_snapshot  # noqa: F401, E402

from alt_pizza_source import load_alt_series_rows


def _cfg() -> dict[str, str]:
    return {
        "url": os.environ.get("LEON_PIZZA_SOURCE_URL", "").strip()
        or "https://www.pizzint.watch/api/neh-index",
        "root_key": os.environ.get("LEON_PIZZA_JSON_ROOT_KEY", "data").strip() or "data",
        "date_col": os.environ.get("LEON_PIZZA_JSON_DATE_FIELD", "date").strip() or "date",
        "value_col": os.environ.get("LEON_PIZZA_JSON_VALUE_FIELD", "value").strip() or "value",
        "series_col": os.environ.get("LEON_PIZZA_JSON_SERIES_FIELD", "series_id").strip() or "series_id",
        "scale": os.environ.get("LEON_PIZZA_SOURCE_SCALE", "1.0").strip() or "1.0",
        "series_id": os.environ.get("LEON_PIZZA_TEST_SERIES_ID", "NOTHING_EVER_HAPPENS_INDEX").strip()
        or "NOTHING_EVER_HAPPENS_INDEX",
    }


def _fetch_payload(url: str):
    with urlopen(url, timeout=20) as r:
        body = r.read().decode("utf-8", errors="replace")
    return json.loads(body), len(body)


def _extract_df(payload, root_key: str) -> pd.DataFrame:
    if isinstance(payload, list):
        data = payload
    elif isinstance(payload, dict) and isinstance(payload.get(root_key), list):
        data = payload.get(root_key)
    else:
        return pd.DataFrame()
    return pd.DataFrame(data)


def _run() -> int:
    cfg = _cfg()
    if not cfg["url"]:
        print("FAIL: LEON_PIZZA_SOURCE_URL is empty.")
        return 2
    if "your-api" in cfg["url"]:
        print(
            "FAIL: LEON_PIZZA_SOURCE_URL is still a placeholder "
            f"({cfg['url']}). Please replace with a real API endpoint."
        )
        return 2

    print("Checking pizza source...")
    print(
        f"URL={cfg['url']}\n"
        f"root={cfg['root_key']} date_col={cfg['date_col']} value_col={cfg['value_col']} "
        f"series_col={cfg['series_col']} scale={cfg['scale']} series_id={cfg['series_id']}"
    )

    try:
        payload, bytes_len = _fetch_payload(cfg["url"])
    except JSONDecodeError as exc:
        print(
            "WARN: URL response is not JSON (likely HTML dashboard page, not an API endpoint).\n"
            f"JSON parse error: {exc}\n"
            "Trying fallback HTML parser (DOUGHCON extraction)..."
        )
        end = date.today()
        start = end - timedelta(days=365 * 5)
        rows = load_alt_series_rows(cfg["series_id"], start, end)
        if rows:
            print(f"OK: fallback parser produced {len(rows)} row(s). Sample: {rows[0]}")
            return 0
        print("FAIL: fallback parser also failed (cannot extract DOUGHCON).")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: URL fetch/network error: {exc}")
        return 2

    print(f"OK: URL reachable, payload bytes={bytes_len}, payload_type={type(payload).__name__}")
    if isinstance(payload, dict) and "global_index" in payload:
        print(
            f"OK: detected NEH endpoint shape: global_index={payload.get('global_index')} "
            f"timestamp={payload.get('timestamp')}"
        )
        end = date.today()
        start = end - timedelta(days=365 * 5)
        rows = load_alt_series_rows(cfg["series_id"], start, end)
        if rows:
            print(f"OK: normalized rows={len(rows)} in [{start}..{end}]")
            print(f"Sample: {rows[0]}")
            return 0
        print("FAIL: could not normalize NEH endpoint payload.")
        return 2

    df = _extract_df(payload, cfg["root_key"])
    if df.empty:
        print(
            "FAIL: JSON shape mismatch. Expect list at root, or object with list in "
            f"'{cfg['root_key']}'."
        )
        return 2

    print(f"OK: extracted rows={len(df)}, columns={list(df.columns)}")
    missing = [c for c in (cfg["date_col"], cfg["value_col"]) if c not in df.columns]
    if missing:
        print(f"FAIL: missing required fields: {missing}")
        return 2

    # End-to-end normalization using production loader
    end = date.today()
    start = end - timedelta(days=365 * 5)
    rows = load_alt_series_rows(cfg["series_id"], start, end)
    if not rows:
        print(
            "WARN: loader returned 0 rows in the 5y window. "
            "Check date format, value numeric conversion, and series filtering."
        )
        return 1

    print(f"OK: normalized rows={len(rows)} in [{start}..{end}]")
    print("Sample rows:")
    for r in rows[:3]:
        print(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(_run())

