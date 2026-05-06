"""Load index earnings-growth proxy series for macro valuation models.

Series:
- SP500_EARNINGS_GROWTH  -> SPY quarterly EPS YoY growth (%)
- NASDAQ_EARNINGS_GROWTH -> QQQ quarterly EPS YoY growth (%)

Preferred source: Alpha Vantage EARNINGS endpoint
Fallback source: FMP quarterly income statement endpoint
"""

from __future__ import annotations

from datetime import date
import json
import os
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

SERIES_TO_SYMBOL = {
    "SP500_EARNINGS_GROWTH": "SPY",
    "NASDAQ_EARNINGS_GROWTH": "QQQ",
}


def _http_json(url: str) -> dict | list | None:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=25) as r:
            body = r.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError):
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _alpha_vantage_eps_df(symbol: str) -> pd.DataFrame:
    key = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
    if not key:
        return pd.DataFrame()
    qs = urlencode({"function": "EARNINGS", "symbol": symbol, "apikey": key})
    payload = _http_json(f"https://www.alphavantage.co/query?{qs}")
    if not isinstance(payload, dict):
        return pd.DataFrame()
    rows = payload.get("quarterlyEarnings")
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "fiscalDateEnding" not in df.columns:
        return pd.DataFrame()
    eps_col = "reportedEPS" if "reportedEPS" in df.columns else "estimatedEPS" if "estimatedEPS" in df.columns else None
    if eps_col is None:
        return pd.DataFrame()
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df["fiscalDateEnding"], errors="coerce"),
            "eps": pd.to_numeric(df[eps_col], errors="coerce"),
        }
    ).dropna()
    return out.sort_values("date")


def _fmp_eps_df(symbol: str) -> pd.DataFrame:
    key = os.environ.get("FMP_API_KEY", "").strip()
    if not key:
        return pd.DataFrame()
    qs = urlencode({"period": "quarter", "limit": "40", "apikey": key})
    payload = _http_json(f"https://financialmodelingprep.com/api/v3/income-statement/{symbol}?{qs}")
    if not isinstance(payload, list) or not payload:
        return pd.DataFrame()
    df = pd.DataFrame(payload)
    if "date" not in df.columns:
        return pd.DataFrame()
    eps_col = "eps" if "eps" in df.columns else "epsdiluted" if "epsdiluted" in df.columns else None
    if eps_col is None:
        return pd.DataFrame()
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df["date"], errors="coerce"),
            "eps": pd.to_numeric(df[eps_col], errors="coerce"),
        }
    ).dropna()
    return out.sort_values("date")


def _to_growth_rows(series_id: str, eps_df: pd.DataFrame, start_date: date, end_date: date) -> list[dict]:
    if eps_df.empty:
        return []
    # Quarterly YoY EPS growth: compare with the same quarter last year.
    eps_df = eps_df.copy().sort_values("date")
    eps_df["eps_yoy"] = eps_df["eps"].pct_change(4) * 100.0
    eps_df = eps_df.dropna(subset=["eps_yoy"])
    eps_df = eps_df[(eps_df["date"].dt.date >= start_date) & (eps_df["date"].dt.date <= end_date)]
    if eps_df.empty:
        return []

    rows: list[dict] = []
    for _, r in eps_df.iterrows():
        rows.append(
            {
                "provider": "alt",
                "series_id": series_id,
                "observation_date": r["date"].date(),
                "value": float(r["eps_yoy"]),
                "value_text": None,
                "vintage_date": date.today(),
                "released_at": None,
            }
        )
    return rows


def load_earnings_growth_rows(series_id: str, start_date: date, end_date: date) -> list[dict]:
    symbol = SERIES_TO_SYMBOL.get(series_id)
    if not symbol:
        return []
    eps_df = _alpha_vantage_eps_df(symbol)
    if eps_df.empty:
        eps_df = _fmp_eps_df(symbol)
    return _to_growth_rows(series_id, eps_df, start_date, end_date)

