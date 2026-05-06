"""Build macro-driven valuation scores for S&P 500 and Nasdaq Composite.

Goal:
- Estimate "rich/cheap" regime using both price position and macro pressure.
- Focus on SP500 and NASDAQCOM only.

Scoring:
- level_percentile_5y: rolling percentile rank of index level over 5 years.
- macro_pressure_score: weighted percentile of real rate / credit spread / NFCI.
- valuation_score: 100 * (0.70 * level_percentile_5y + 0.30 * macro_pressure_score)

Higher valuation_score => relatively richer / more expensive conditions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import pandas as pd
from sqlalchemy import delete, select

THIS_DIR = Path(__file__).resolve().parent
LEON_DIR = THIS_DIR.parent
DATA_DIR = LEON_DIR / "data"
if str(DATA_DIR) not in sys.path:
    sys.path.insert(0, str(DATA_DIR))

# Side effect: load config.env / .env
import ingest_daily_snapshot  # noqa: F401, E402

from database import init_db, session_scope
from models import DailyQuote, MacroObservation, MacroValuationScore

INDEX_CANDIDATES: dict[str, list[str]] = {
    "SP500": ["SP500", "SPY"],
    "NASDAQ": ["NASDAQCOM", "NASDAQ100", "QQQ"],
}
MACRO_SERIES = ["DGS10", "CPIAUCSL", "NFCI", "BAMLH0A0HYM2"]
EARNINGS_GROWTH_SERIES = {
    "SP500": "SP500_EARNINGS_GROWTH",
    "NASDAQ": "NASDAQ_EARNINGS_GROWTH",
}
EARNINGS_FALLBACK_CANDIDATES = [
    "CP",  # Corporate Profits, best broad earnings-cycle proxy
    "INDPRO",  # Industrial production, cyclic earnings proxy
]
NEEDED = sorted({s for v in INDEX_CANDIDATES.values() for s in v} | set(MACRO_SERIES))


def _pct_rank(s: pd.Series, window: int, min_periods: int) -> pd.Series:
    def rank_last(x):
        r = pd.Series(x).rank(pct=True)
        return float(r.iloc[-1]) if len(r) else float("nan")

    return s.rolling(window=window, min_periods=min_periods).apply(rank_last, raw=False)


def _valuation_zone(x: float) -> str:
    if x >= 80:
        return "very_high"
    if x >= 65:
        return "high"
    if x >= 40:
        return "neutral"
    if x >= 25:
        return "low"
    return "very_low"


def _load_wide_df(session) -> pd.DataFrame:
    macro_needed = set(NEEDED) | set(EARNINGS_GROWTH_SERIES.values()) | set(EARNINGS_FALLBACK_CANDIDATES)
    rows = session.execute(
        select(MacroObservation.observation_date, MacroObservation.series_id, MacroObservation.value).where(
            MacroObservation.provider == "fred",
            MacroObservation.series_id.in_(sorted(macro_needed)),
        )
    ).all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["date", "series_id", "value"])
    wide = df.pivot_table(index="date", columns="series_id", values="value", aggfunc="last").sort_index()
    wide.index = pd.to_datetime(wide.index, errors="coerce")
    wide = wide[~wide.index.isna()].sort_index()

    # Fallback index level sources from daily_quotes proxies:
    # - SPY as S&P 500 tradable proxy
    # - QQQ as Nasdaq-100 tradable proxy
    q_rows = session.execute(
        select(DailyQuote.trade_date, DailyQuote.symbol, DailyQuote.close).where(DailyQuote.symbol.in_(["SPY", "QQQ"]))
    ).all()
    if q_rows:
        q_df = pd.DataFrame(q_rows, columns=["date", "symbol", "close"])
        q_wide = q_df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
        q_wide.index = pd.to_datetime(q_wide.index, errors="coerce")
        q_wide = q_wide[~q_wide.index.isna()].sort_index()
        wide = wide.combine_first(q_wide)

    return wide


def _build_scores(wide: pd.DataFrame) -> pd.DataFrame:
    if wide.empty:
        return pd.DataFrame()

    daily = wide.resample("D").last().ffill()
    # Monthly CPI -> YoY inflation (%) on month-end, then daily-forward-fill.
    cpi_m = daily.get("CPIAUCSL").resample("ME").last()
    cpi_yoy_m = cpi_m.pct_change(12) * 100.0
    cpi_yoy_d = cpi_yoy_m.reindex(daily.index, method="ffill")
    real_10y = daily.get("DGS10") - cpi_yoy_d

    p_real_10y = _pct_rank(real_10y, window=1260, min_periods=252)
    p_credit = _pct_rank(daily.get("BAMLH0A0HYM2"), window=1260, min_periods=252)
    p_nfci = _pct_rank(daily.get("NFCI"), window=1260, min_periods=252)
    macro_pressure = 0.40 * p_real_10y + 0.35 * p_credit + 0.25 * p_nfci

    # Earnings-growth factor:
    # - preferred: index-specific YoY earnings growth series (if user later lands it)
    # - fallback: CP YoY as broad earnings-cycle proxy
    fallback_earn_source = next((s for s in EARNINGS_FALLBACK_CANDIDATES if s in daily.columns), None)
    if not fallback_earn_source:
        return pd.DataFrame()
    fallback_earn_yoy_m = daily.get(fallback_earn_source).resample("ME").last().pct_change(12) * 100.0
    fallback_earn_yoy_d = fallback_earn_yoy_m.reindex(daily.index, method="ffill")

    out_frames: list[pd.DataFrame] = []
    for index_code, candidates in INDEX_CANDIDATES.items():
        source_series = next((name for name in candidates if name in daily.columns), None)
        if not source_series:
            continue
        s = daily.get(source_series)
        if s is None:
            continue
        p_level = _pct_rank(s, window=1260, min_periods=252)
        earn_series = EARNINGS_GROWTH_SERIES[index_code]
        earn_source = fallback_earn_source
        if earn_series in daily.columns:
            earn_yoy_d = daily.get(earn_series)
            earn_source = earn_series
        else:
            earn_yoy_d = fallback_earn_yoy_d
        p_earn_growth = _pct_rank(earn_yoy_d, window=1260, min_periods=252)

        # Higher earnings growth should reduce "expensive" score.
        earnings_cheapness = 1.0 - p_earn_growth
        valuation = 100.0 * (0.60 * p_level + 0.25 * macro_pressure + 0.15 * earnings_cheapness)
        part = pd.DataFrame(
            {
                "index_code": index_code,
                "source_series": source_series,
                "earnings_source": earn_source,
                "level_value": s,
                "level_percentile_5y": p_level,
                "macro_pressure_score": macro_pressure,
                "earnings_growth_percentile_5y": p_earn_growth,
                "valuation_score": valuation,
            }
        ).dropna()
        out_frames.append(part)

    if not out_frames:
        return pd.DataFrame()

    out = pd.concat(out_frames).sort_index()
    out["valuation_zone"] = out["valuation_score"].map(_valuation_zone)
    return out


def _run():
    engines = init_db()
    with session_scope(engines[0]) as session:
        wide = _load_wide_df(session)

    missing_macro = [s for s in MACRO_SERIES if s not in wide.columns]
    if missing_macro:
        print("Missing required macro series in macro_observations:", ", ".join(missing_macro))
        print("Run macro_series_seed.py + ingest_macro_incremental.py first.")
        return
    if not any(s in wide.columns for s in EARNINGS_FALLBACK_CANDIDATES):
        print(f"Missing earnings fallback series: one of {EARNINGS_FALLBACK_CANDIDATES}")
        print("Run macro_series_seed.py + ingest_macro_incremental.py first.")
        return
    for idx, candidates in INDEX_CANDIDATES.items():
        if not any(s in wide.columns for s in candidates):
            print(f"Missing index series for {idx}: need one of {candidates}")
            print("Run macro_series_seed.py + ingest_macro_incremental.py first.")
            return

    out = _build_scores(wide)
    if out.empty:
        print("No valuation rows computed (insufficient history).")
        return

    for eng in engines:
        with session_scope(eng) as session:
            session.execute(delete(MacroValuationScore))
            for idx, r in out.iterrows():
                session.add(
                    MacroValuationScore(
                        score_date=idx.date(),
                        index_code=str(r["index_code"]),
                        valuation_score=float(r["valuation_score"]),
                        level_value=float(r["level_value"]) if pd.notna(r["level_value"]) else None,
                        level_percentile_5y=(
                            float(r["level_percentile_5y"]) if pd.notna(r["level_percentile_5y"]) else None
                        ),
                        macro_pressure_score=(
                            float(r["macro_pressure_score"]) if pd.notna(r["macro_pressure_score"]) else None
                        ),
                        valuation_zone=str(r["valuation_zone"]),
                        components_json={
                            "weight_level": 0.60,
                            "weight_macro_pressure": 0.25,
                            "weight_earnings_cheapness": 0.15,
                            "source_series": str(r["source_series"]),
                            "earnings_source": str(r["earnings_source"]),
                            "earnings_growth_percentile_5y": (
                                float(r["earnings_growth_percentile_5y"])
                                if pd.notna(r["earnings_growth_percentile_5y"])
                                else None
                            ),
                            "macro_weights": {
                                "real_10y": 0.40,
                                "hy_oas": 0.35,
                                "nfci": 0.25,
                            },
                        },
                        updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                )
            session.commit()

    latest = out.reset_index().sort_values("date").groupby("index_code", as_index=False).tail(1)
    print(f"Macro valuation score built. rows={len(out)}")
    for _, r in latest.iterrows():
        print(
            f"  {r['index_code']}: date={r['date'].date()}, score={r['valuation_score']:.2f}, "
            f"level_pct_5y={r['level_percentile_5y']:.2f}, "
            f"earn_src={r['earnings_source']}, zone={r['valuation_zone']}"
        )


if __name__ == "__main__":
    _run()

