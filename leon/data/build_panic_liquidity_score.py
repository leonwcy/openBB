"""Build 0-100 panic/liquidity composite scores from macro_observations.

Formula (higher = more stress):
  p_vix   = percentile_rank_2y(VIXCLS)
  p_hy    = percentile_rank_2y(BAMLH0A0HYM2)
  p_ig    = percentile_rank_2y(BAMLC0A0CM)
  p_nfci  = percentile_rank_2y(NFCI)
  p_ted   = percentile_rank_2y(TEDRATE)
  p_repo  = percentile_rank_2y(SOFR - DFF)

  panic_score     = 100 * (0.30*p_vix + 0.25*p_hy + 0.15*p_ig + 0.15*p_nfci + 0.10*p_ted + 0.05*p_repo)
  liquidity_score = 100 * (0.35*p_ted + 0.25*p_repo + 0.20*p_nfci + 0.20*p_hy)

Regime:
  >= 75: extreme_stress
  >= 55: stress
  >= 35: neutral
  else: calm
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import pandas as pd
from sqlalchemy import delete, select

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

# Side effect: load config.env / .env so DATABASE_URL points to Postgres in scheduled/manual runs.
import ingest_daily_snapshot  # noqa: F401, E402

from database import get_engines, init_db, session_scope
from models import MacroObservation, PanicLiquidityScore

NEEDED = ["VIXCLS", "BAMLH0A0HYM2", "BAMLC0A0CM", "NFCI", "TEDRATE", "SOFR", "DFF"]


def _to_wide_df(session) -> pd.DataFrame:
    rows = session.execute(
        select(MacroObservation.observation_date, MacroObservation.series_id, MacroObservation.value).where(
            MacroObservation.provider == "fred",
            MacroObservation.series_id.in_(NEEDED),
        )
    ).all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["date", "series_id", "value"])
    wide = df.pivot_table(index="date", columns="series_id", values="value", aggfunc="last").sort_index()
    return wide


def _pct_rank(s: pd.Series, window: int = 504) -> pd.Series:
    # Rolling percentile rank in [0,1].
    def rank_last(x):
        r = pd.Series(x).rank(pct=True)
        return float(r.iloc[-1]) if len(r) else float("nan")

    return s.rolling(window=window, min_periods=max(60, window // 6)).apply(rank_last, raw=False)


def _regime(x: float) -> str:
    if x >= 75:
        return "extreme_stress"
    if x >= 55:
        return "stress"
    if x >= 35:
        return "neutral"
    return "calm"


def _build_scores(wide: pd.DataFrame) -> pd.DataFrame:
    if wide.empty:
        return pd.DataFrame()

    wide = wide.copy()
    wide.index = pd.to_datetime(wide.index, errors="coerce")
    wide = wide[~wide.index.isna()].sort_index()
    if wide.empty:
        return pd.DataFrame()

    # Align daily index and fill slower series forward.
    daily = wide.resample("D").last().ffill()

    # Repo pressure proxy.
    daily["SOFR_DFF_SPREAD"] = daily.get("SOFR") - daily.get("DFF")

    p_vix = _pct_rank(daily.get("VIXCLS"))
    p_hy = _pct_rank(daily.get("BAMLH0A0HYM2"))
    p_ig = _pct_rank(daily.get("BAMLC0A0CM"))
    p_nfci = _pct_rank(daily.get("NFCI"))
    p_ted = _pct_rank(daily.get("TEDRATE"))
    p_repo = _pct_rank(daily.get("SOFR_DFF_SPREAD"))

    panic = 100.0 * (
        0.30 * p_vix + 0.25 * p_hy + 0.15 * p_ig + 0.15 * p_nfci + 0.10 * p_ted + 0.05 * p_repo
    )
    liq = 100.0 * (0.35 * p_ted + 0.25 * p_repo + 0.20 * p_nfci + 0.20 * p_hy)

    out = pd.DataFrame({"panic_score": panic, "liquidity_score": liq})
    out = out.dropna()
    if out.empty:
        return out
    out["regime"] = out["panic_score"].map(_regime)
    out["p_vix"] = p_vix
    out["p_hy"] = p_hy
    out["p_ig"] = p_ig
    out["p_nfci"] = p_nfci
    out["p_ted"] = p_ted
    out["p_repo"] = p_repo
    return out


def _run():
    engines = init_db()
    with session_scope(engines[0]) as session:
        wide = _to_wide_df(session)
    out = _build_scores(wide)
    if out.empty:
        print("No score rows computed (missing source history).")
        return

    for eng in engines:
        with session_scope(eng) as session:
            session.execute(delete(PanicLiquidityScore))
            for idx, r in out.iterrows():
                session.add(
                    PanicLiquidityScore(
                        score_date=idx.date(),
                        panic_score=float(r["panic_score"]),
                        liquidity_score=float(r["liquidity_score"]),
                        regime=str(r["regime"]),
                        components_json={
                            "p_vix": None if pd.isna(r["p_vix"]) else float(r["p_vix"]),
                            "p_hy": None if pd.isna(r["p_hy"]) else float(r["p_hy"]),
                            "p_ig": None if pd.isna(r["p_ig"]) else float(r["p_ig"]),
                            "p_nfci": None if pd.isna(r["p_nfci"]) else float(r["p_nfci"]),
                            "p_ted": None if pd.isna(r["p_ted"]) else float(r["p_ted"]),
                            "p_repo": None if pd.isna(r["p_repo"]) else float(r["p_repo"]),
                        },
                        updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                )
            session.commit()

    latest = out.iloc[-1]
    print(
        "Panic/liquidity score built. "
        f"rows={len(out)}, latest_date={out.index[-1].date()}, "
        f"panic={latest['panic_score']:.2f}, liquidity={latest['liquidity_score']:.2f}, "
        f"regime={latest['regime']}"
    )


if __name__ == "__main__":
    _run()

