"""Alternative macro series loaders (e.g. Nothing Ever Happens Index).

This module intentionally avoids CSV files and expects API/JSON payloads.
"""

from __future__ import annotations

from datetime import date
import json
import os
import re
from urllib.request import urlopen

import pandas as pd


def _source_url() -> str | None:
    raw = os.environ.get("LEON_PIZZA_SOURCE_URL", "").strip()
    # Default to known JSON endpoint.
    return raw or "https://www.pizzint.watch/api/neh-index"


def _history_url() -> str:
    raw = os.environ.get("LEON_PIZZA_HISTORY_URL", "").strip()
    return raw or "https://www.pizzint.watch/api/neh-index/history"


def _fallback_page_url() -> str:
    return os.environ.get("LEON_PIZZA_FALLBACK_URL", "https://www.pizzint.watch").strip() or "https://www.pizzint.watch"


def _fallback_value(doughcon_level: int) -> float:
    # Keep value monotonic with stress by default: DOUGHCON 1 -> 5.0 (high stress), 5 -> 1.0 (low stress)
    invert = os.environ.get("LEON_PIZZA_FALLBACK_INVERT", "1").strip().lower() in ("1", "true", "yes")
    return float(6 - doughcon_level) if invert else float(doughcon_level)


def _load_from_fallback_page(series_id: str, start_date: date, end_date: date) -> list[dict]:
    """Fallback parser: extract DOUGHCON level from homepage HTML."""
    try:
        with urlopen(_fallback_page_url(), timeout=20) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []

    m = re.search(r"\bDOUGHCON\s*([1-5])\b", html, flags=re.IGNORECASE)
    if not m:
        return []
    level = int(m.group(1))
    today = date.today()
    if today < start_date or today > end_date:
        return []
    return [
        {
            "provider": "alt",
            "series_id": series_id,
            "observation_date": today,
            "value": _fallback_value(level),
            "value_text": f"DOUGHCON {level}",
            "vintage_date": date.today(),
            "released_at": None,
        }
    ]


def _load_neh_history_rows(series_id: str, start_date: date, end_date: date, scale: float) -> list[dict]:
    """Load NEH history endpoint and normalize to one row per day (last observation)."""
    try:
        with urlopen(_history_url(), timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []

    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        data = payload["data"]
    elif isinstance(payload, list):
        data = payload
    else:
        return []

    if not data:
        return []

    df = pd.DataFrame(data)
    if "timestamp" not in df.columns:
        return []

    # Common history fields observed:
    # - timestamp: ISO datetime
    # - value: numeric index value
    if "value" not in df.columns:
        return []

    ts = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    vals = pd.to_numeric(df["value"], errors="coerce")
    out = pd.DataFrame({"ts": ts, "v": vals}).dropna().sort_values("ts")
    if out.empty:
        return []

    out["d"] = out["ts"].dt.date
    out = out[(out["d"] >= start_date) & (out["d"] <= end_date)]
    if out.empty:
        return []

    # Keep one value per day to avoid minute-level duplicate writes.
    daily = out.groupby("d", as_index=False).last()
    rows: list[dict] = []
    for _, r in daily.iterrows():
        rows.append(
            {
                "provider": "alt",
                "series_id": series_id,
                "observation_date": r["d"],
                "value": float(r["v"]) * scale,
                "value_text": None,
                "vintage_date": date.today(),
                "released_at": None,
            }
        )
    return rows


def load_alt_series_rows(series_id: str, start_date: date, end_date: date) -> list[dict]:
    """Load an alternative series from JSON API and normalize to macro rows.

    Supported payload:
    - root list of objects
    - object containing list at key configured by LEON_PIZZA_JSON_ROOT_KEY (default: data)

    Expected fields (configurable):
    - date field (default: date)
    - value field (default: value)
    - optional series field (default: series_id)
    """
    url = _source_url()
    if not url:
        # No JSON URL configured -> fallback to webpage parsing.
        return _load_from_fallback_page(series_id, start_date, end_date)

    date_col = os.environ.get("LEON_PIZZA_JSON_DATE_FIELD", "date").strip() or "date"
    value_col = os.environ.get("LEON_PIZZA_JSON_VALUE_FIELD", "value").strip() or "value"
    series_col = os.environ.get("LEON_PIZZA_JSON_SERIES_FIELD", "series_id").strip() or "series_id"
    root_key = os.environ.get("LEON_PIZZA_JSON_ROOT_KEY", "data").strip() or "data"
    scale = float(os.environ.get("LEON_PIZZA_SOURCE_SCALE", "1.0"))

    try:
        with urlopen(url, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        # URL may be HTML or unavailable -> fallback parser.
        return _load_from_fallback_page(series_id, start_date, end_date)

    # Dedicated NEH endpoint shape: {"global_index": 37, "timestamp": "...", ...}
    if isinstance(payload, dict) and "global_index" in payload:
        history_rows = _load_neh_history_rows(series_id, start_date, end_date, scale)
        if history_rows:
            return history_rows
        ts = pd.to_datetime(payload.get("timestamp"), errors="coerce")
        d = ts.date() if pd.notna(ts) else date.today()
        if start_date <= d <= end_date:
            return [
                {
                    "provider": "alt",
                    "series_id": "NOTHING_EVER_HAPPENS_INDEX",
                    "observation_date": d,
                    "value": float(payload.get("global_index")) * scale,
                    "value_text": payload.get("label"),
                    "vintage_date": date.today(),
                    "released_at": None,
                }
            ]
        return []

    if isinstance(payload, list):
        data = payload
    elif isinstance(payload, dict) and isinstance(payload.get(root_key), list):
        data = payload.get(root_key)
    else:
        return _load_from_fallback_page(series_id, start_date, end_date)

    df = pd.DataFrame(data)
    if df.empty:
        return []
    if series_col in df.columns:
        df = df[df[series_col].astype(str).str.upper() == series_id.upper()]
    if date_col not in df.columns or value_col not in df.columns:
        return []

    dts = pd.to_datetime(df[date_col], errors="coerce")
    vals = pd.to_numeric(df[value_col], errors="coerce")
    out = pd.DataFrame({"d": dts, "v": vals}).dropna().sort_values("d")
    if out.empty:
        return _load_from_fallback_page(series_id, start_date, end_date)

    out = out[(out["d"].dt.date >= start_date) & (out["d"].dt.date <= end_date)]
    if out.empty:
        return []

    rows: list[dict] = []
    for _, r in out.iterrows():
        rows.append(
            {
                "provider": "alt",
                "series_id": series_id,
                "observation_date": r["d"].date(),
                "value": float(r["v"]) * scale,
                "value_text": None,
                "vintage_date": date.today(),
                "released_at": None,
            }
        )
    return rows

