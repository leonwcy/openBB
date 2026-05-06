"""Liquidity score dashboard for leon platform.

Run:
    streamlit run leon/platform/app.py
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import json
import os

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "leon" / "data"
LIQ_FACTORS = ["SOFR", "DFF", "NFCI", "BAMLH0A0HYM2", "BAMLC0A0CM", "VIXCLS"]
LIQ_WEIGHTS = {"p_repo": 0.45, "p_nfci": 0.25, "p_hy": 0.30}
FACTOR_PLOT_ORDER = [
    ("SOFR_DFF_SPREAD", "SOFR - DFF Spread"),
    ("NFCI", "NFCI"),
    ("BAMLH0A0HYM2", "HY OAS"),
    ("BAMLC0A0CM", "IG OAS"),
    ("VIXCLS", "VIX"),
]
FACTOR_GUARDRAILS = {
    # Values above red line indicate tighter liquidity / stress.
    # Values near or below green line indicate relatively calmer backdrop.
    "SOFR_DFF_SPREAD": {"warn": 0.25, "safe": 0.05, "unit": "%"},
    "NFCI": {"warn": 0.50, "safe": -0.25, "unit": "index"},
    "BAMLH0A0HYM2": {"warn": 5.0, "safe": 3.5, "unit": "%"},
    "BAMLC0A0CM": {"warn": 2.0, "safe": 1.2, "unit": "%"},
    "VIXCLS": {"warn": 25.0, "safe": 18.0, "unit": "index"},
}
PIZZA_SERIES_CANDIDATES = ["PIZZA"]


def _load_env() -> None:
    candidates = [
        DATA_DIR / "config.env",
        DATA_DIR / ".env",
        Path.cwd() / "config.env",
        Path.cwd() / ".env",
    ]
    for p in candidates:
        if p.is_file():
            try:
                load_dotenv(p, override=True, encoding="utf-8-sig")
            except TypeError:
                load_dotenv(p, override=True)


def _database_url() -> str:
    _load_env()
    db = os.environ.get("DATABASE_URL", "").strip()
    if not db:
        default_sqlite = DATA_DIR / "market.db"
        db = f"sqlite:///{default_sqlite.as_posix()}"
    return db


def _cutoff_for_range(label: str) -> date | None:
    today = date.today()
    mapping = {
        "1M": today - timedelta(days=31),
        "3M": today - timedelta(days=92),
        "6M": today - timedelta(days=183),
        "1Y": today - timedelta(days=366),
        "3Y": today - timedelta(days=366 * 3),
        "5Y": today - timedelta(days=366 * 5),
        "ALL": None,
    }
    return mapping[label]


def _load_scores(range_label: str) -> pd.DataFrame:
    cutoff = _cutoff_for_range(range_label)
    sql = (
        "select score_date, panic_score, liquidity_score, regime, components_json "
        "from panic_liquidity_scores "
        + ("where score_date >= :cutoff " if cutoff else "")
        + "order by score_date"
    )
    engine = create_engine(_database_url(), future=True)
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"cutoff": cutoff} if cutoff else {}).fetchall()
    if not rows:
        return pd.DataFrame(columns=["score_date", "panic_score", "liquidity_score", "regime", "components_json"])
    df = pd.DataFrame(rows, columns=["score_date", "panic_score", "liquidity_score", "regime", "components_json"])
    df["score_date"] = pd.to_datetime(df["score_date"])
    df["components_json"] = df["components_json"].apply(
        lambda v: json.loads(v) if isinstance(v, str) and v.startswith("{") else (v if isinstance(v, dict) else {})
    )
    return df


def _load_liquidity_factors(range_label: str) -> pd.DataFrame:
    cutoff = _cutoff_for_range(range_label)
    series_sql = ", ".join(f"'{s}'" for s in LIQ_FACTORS)
    sql = (
        "select observation_date, series_id, value "
        "from macro_observations "
        f"where provider = 'fred' and series_id in ({series_sql}) "
        + ("and observation_date >= :cutoff " if cutoff else "")
        + "order by observation_date"
    )
    engine = create_engine(_database_url(), future=True)
    params: dict[str, object] = {}
    if cutoff:
        params["cutoff"] = cutoff
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["date", "series_id", "value"])
    wide = df.pivot_table(index="date", columns="series_id", values="value", aggfunc="last").sort_index()
    wide.index = pd.to_datetime(wide.index, errors="coerce")
    wide = wide[~wide.index.isna()].sort_index()
    if wide.empty:
        return pd.DataFrame()
    daily = wide.resample("D").last().ffill()
    daily["SOFR_DFF_SPREAD"] = daily.get("SOFR") - daily.get("DFF")
    return daily


def _load_pizza_series(range_label: str) -> pd.DataFrame:
    cutoff = _cutoff_for_range(range_label)
    series_sql = ", ".join(f"'{s}'" for s in PIZZA_SERIES_CANDIDATES)
    sql = (
        "select observation_date, series_id, value "
        "from macro_observations "
        f"where provider = 'alt' and series_id in ({series_sql}) and value is not null "
        + ("and observation_date >= :cutoff " if cutoff else "")
        + "order by observation_date"
    )
    engine = create_engine(_database_url(), future=True)
    params: dict[str, object] = {}
    if cutoff:
        params["cutoff"] = cutoff
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    if not rows:
        return pd.DataFrame(columns=["date", "series_id", "value"])
    df = pd.DataFrame(rows, columns=["date", "series_id", "value"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")

    # Pick one preferred series to avoid mixed signal plotting.
    picked = None
    for sid in PIZZA_SERIES_CANDIDATES:
        part = df[df["series_id"] == sid]
        if not part.empty:
            picked = part
            break
    if picked is None:
        return pd.DataFrame(columns=["date", "series_id", "value"])
    return picked.reset_index(drop=True)


def _factor_anomalies(series: pd.Series, window: int = 30, z_threshold: float = 2.5) -> pd.DataFrame:
    """Detect abrupt daily moves using rolling z-score on first differences."""
    s = series.dropna()
    if len(s) < 15:
        return pd.DataFrame(columns=["date", "value", "delta", "z", "direction"])
    delta = s.diff()
    mu = delta.rolling(window=window, min_periods=max(10, window // 3)).mean()
    sigma = delta.rolling(window=window, min_periods=max(10, window // 3)).std()
    z = (delta - mu) / sigma
    out = pd.DataFrame({"date": s.index, "value": s.values, "delta": delta.values, "z": z.values}).dropna()
    out = out[out["z"].abs() >= z_threshold].copy()
    if out.empty:
        return pd.DataFrame(columns=["date", "value", "delta", "z", "direction"])
    out["direction"] = out["delta"].apply(lambda x: "spike_up" if x > 0 else "spike_down")
    return out


def _factor_trend_chart(series: pd.Series, title: str, factor_key: str | None = None):
    s = series.dropna()
    if s.empty:
        st.caption(f"{title}: no data")
        return
    anomalies = _factor_anomalies(s)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=s.index, y=s.values, mode="lines", name=title, line=dict(width=2)))
    guard = FACTOR_GUARDRAILS.get(factor_key or "", None)
    if guard:
        fig.add_hline(
            y=guard["warn"],
            line_dash="dash",
            line_color="#d62728",
            annotation_text=f"Warn {guard['warn']}",
            annotation_position="top left",
        )
        fig.add_hline(
            y=guard["safe"],
            line_dash="dot",
            line_color="#2ca02c",
            annotation_text=f"Safe {guard['safe']}",
            annotation_position="bottom left",
        )
    if not anomalies.empty:
        up = anomalies[anomalies["direction"] == "spike_up"]
        down = anomalies[anomalies["direction"] == "spike_down"]
        if not up.empty:
            fig.add_trace(
                go.Scatter(
                    x=up["date"],
                    y=up["value"],
                    mode="markers",
                    name="Spike Up",
                    marker=dict(color="#d62728", size=8, symbol="triangle-up"),
                    hovertemplate="Date=%{x}<br>Value=%{y:.4f}<br>Delta=%{customdata[0]:.4f}<br>Z=%{customdata[1]:.2f}<extra></extra>",
                    customdata=up[["delta", "z"]].to_numpy(),
                )
            )
        if not down.empty:
            fig.add_trace(
                go.Scatter(
                    x=down["date"],
                    y=down["value"],
                    mode="markers",
                    name="Spike Down",
                    marker=dict(color="#1f77b4", size=8, symbol="triangle-down"),
                    hovertemplate="Date=%{x}<br>Value=%{y:.4f}<br>Delta=%{customdata[0]:.4f}<br>Z=%{customdata[1]:.2f}<extra></extra>",
                    customdata=down[["delta", "z"]].to_numpy(),
                )
            )
    fig.update_layout(
        title=title,
        height=360,
        margin=dict(l=10, r=10, t=45, b=20),
        legend_title="",
        xaxis_title="Date",
        yaxis_title="",
    )
    st.plotly_chart(fig, use_container_width=True)
    if anomalies.empty:
        st.caption("No significant spikes/drops detected under current threshold (|z| >= 2.5).")
    else:
        up_n = int((anomalies["direction"] == "spike_up").sum())
        down_n = int((anomalies["direction"] == "spike_down").sum())
        st.caption(f"Highlighted anomalies: spike_up={up_n}, spike_down={down_n}")
    if guard:
        st.caption(
            f"Guardrails ({guard['unit']}): red warn={guard['warn']}, green safe={guard['safe']}. "
            "Use as monitoring reference, not hard trading rules."
        )


def main() -> None:
    st.set_page_config(page_title="Leon Liquidity Score", layout="wide")
    st.title("Leon Liquidity Score Dashboard")
    st.caption("Data source: panic_liquidity_scores")

    range_label = st.radio(
        "Time Range",
        options=["1M", "3M", "6M", "1Y", "3Y", "5Y", "ALL"],
        index=3,
        horizontal=True,
    )
    df = _load_scores(range_label)

    if df.empty:
        st.warning("No rows found in panic_liquidity_scores for this range.")
        st.stop()

    latest = df.iloc[-1]
    c1, c2, c3 = st.columns(3)
    c1.metric("Latest Date", latest["score_date"].date().isoformat())
    c2.metric("Liquidity Score", f"{latest['liquidity_score']:.2f}")
    c3.metric("Regime", str(latest["regime"]))

    plot_df = df.melt(
        id_vars=["score_date"],
        value_vars=["liquidity_score", "panic_score"],
        var_name="series",
        value_name="score",
    )
    fig = px.line(plot_df, x="score_date", y="score", color="series", title=f"Scores ({range_label})")
    fig.update_layout(yaxis_title="Score (0-100)", xaxis_title="Date", legend_title="")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Liquidity Formula & Weights")
    st.markdown(
        "Liquidity score uses rolling percentile factors:\n"
        "`liquidity_score = 100 * (0.45*p_repo + 0.25*p_nfci + 0.30*p_hy)`\n\n"
        "- `p_repo`: percentile of `SOFR - DFF`\n"
        "- `p_nfci`: percentile of `NFCI`\n"
        "- `p_hy`: percentile of `BAMLH0A0HYM2` (HY OAS)"
    )

    st.subheader("Weight Visualization")
    weights_df = pd.DataFrame(
        [{"component": k, "weight": v} for k, v in LIQ_WEIGHTS.items()]
    ).sort_values("weight", ascending=False)
    wc1, wc2 = st.columns(2)
    with wc1:
        pie = px.pie(weights_df, values="weight", names="component", title="Liquidity Formula Weights")
        pie.update_traces(textposition="inside", textinfo="percent+label")
        wc1.plotly_chart(pie, use_container_width=True)
    with wc2:
        bar = px.bar(weights_df, x="component", y="weight", title="Weight by Component")
        bar.update_layout(yaxis_title="Weight", xaxis_title="")
        wc2.plotly_chart(bar, use_container_width=True)

    factors = _load_liquidity_factors(range_label)
    if not factors.empty:
        st.subheader("Key Liquidity Factors - Trend View")
        st.caption("Each factor chart highlights abrupt spikes/drops using rolling z-score on daily changes.")
        for factor_col, title in FACTOR_PLOT_ORDER:
            if factor_col in factors.columns:
                _factor_trend_chart(factors[factor_col], title, factor_col)

        st.subheader("Latest Factor Values & Percentiles")
        latest_f = factors.iloc[-1]
        comps = latest["components_json"] if isinstance(latest["components_json"], dict) else {}
        detail = pd.DataFrame(
            [
                {"factor": "SOFR_DFF_SPREAD", "latest_value": latest_f.get("SOFR_DFF_SPREAD"), "percentile": comps.get("p_repo")},
                {"factor": "NFCI", "latest_value": latest_f.get("NFCI"), "percentile": comps.get("p_nfci")},
                {"factor": "BAMLH0A0HYM2", "latest_value": latest_f.get("BAMLH0A0HYM2"), "percentile": comps.get("p_hy")},
                {"factor": "SOFR", "latest_value": latest_f.get("SOFR"), "percentile": None},
                {"factor": "DFF", "latest_value": latest_f.get("DFF"), "percentile": None},
            ]
        )
        st.dataframe(detail, use_container_width=True)

    with st.expander("Recent Rows"):
        shown = df[["score_date", "panic_score", "liquidity_score", "regime"]].tail(20)
        st.dataframe(shown, use_container_width=True)

    st.divider()
    st.subheader("PIZZA Index Dashboard")
    pizza_range = st.radio(
        "PIZZA Time Range",
        options=["1M", "3M", "6M", "1Y", "3Y", "5Y", "ALL"],
        index=3,
        horizontal=True,
        key="pizza_time_range",
    )
    pizza = _load_pizza_series(pizza_range)
    if pizza.empty:
        st.info("No PIZZA index rows found in macro_observations (provider=alt).")
    else:
        s = pizza.set_index("date")["value"].astype(float).sort_index()
        latest_val = float(s.iloc[-1])
        latest_date = s.index[-1].date().isoformat()
        d7 = None
        if len(s) >= 8:
            d7 = latest_val - float(s.iloc[-8])
        p1, p2, p3 = st.columns(3)
        p1.metric("Series", str(pizza.iloc[-1]["series_id"]))
        p2.metric("Latest Date", latest_date)
        p3.metric("Latest Value", f"{latest_val:.2f}", delta=(f"{d7:+.2f} vs 7d ago" if d7 is not None else None))

        pizza_anom = _factor_anomalies(s, window=20, z_threshold=2.2)
        pfig = go.Figure()
        pfig.add_trace(go.Scatter(x=s.index, y=s.values, mode="lines", name="PIZZA Index", line=dict(width=2)))
        if not pizza_anom.empty:
            up = pizza_anom[pizza_anom["direction"] == "spike_up"]
            down = pizza_anom[pizza_anom["direction"] == "spike_down"]
            if not up.empty:
                pfig.add_trace(
                    go.Scatter(
                        x=up["date"],
                        y=up["value"],
                        mode="markers",
                        name="Spike Up",
                        marker=dict(color="#d62728", size=9, symbol="triangle-up"),
                        hovertemplate="Date=%{x}<br>Value=%{y:.2f}<br>Delta=%{customdata[0]:.2f}<br>Z=%{customdata[1]:.2f}<extra></extra>",
                        customdata=up[["delta", "z"]].to_numpy(),
                    )
                )
            if not down.empty:
                pfig.add_trace(
                    go.Scatter(
                        x=down["date"],
                        y=down["value"],
                        mode="markers",
                        name="Spike Down",
                        marker=dict(color="#1f77b4", size=9, symbol="triangle-down"),
                        hovertemplate="Date=%{x}<br>Value=%{y:.2f}<br>Delta=%{customdata[0]:.2f}<br>Z=%{customdata[1]:.2f}<extra></extra>",
                        customdata=down[["delta", "z"]].to_numpy(),
                    )
                )
        pfig.update_layout(
            title=f"PIZZA Trend ({pizza_range})",
            yaxis_title="Index Value",
            xaxis_title="Date",
            legend_title="",
            height=420,
        )
        st.plotly_chart(pfig, use_container_width=True)
        if pizza_anom.empty:
            st.caption("No significant spikes/drops detected for PIZZA under current threshold (|z| >= 2.2).")
        else:
            st.caption(
                f"PIZZA anomalies highlighted: spike_up={int((pizza_anom['direction']=='spike_up').sum())}, "
                f"spike_down={int((pizza_anom['direction']=='spike_down').sum())}"
            )


if __name__ == "__main__":
    main()

