"""Macro valuation dashboard render module."""

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
VAL_FACTOR_PLOT_ORDER = [
    ("level_percentile_5y", "Level Percentile (5Y)"),
    ("macro_pressure_score", "Macro Pressure Score"),
    ("earnings_cheapness", "Earnings Cheapness (1 - Earnings Growth Percentile)"),
]
VAL_FACTOR_GUARDRAILS = {
    # Higher values mean richer/more expensive conditions in this scoring setup.
    "level_percentile_5y": {"warn": 0.80, "safe": 0.35, "unit": "ratio"},
    "macro_pressure_score": {"warn": 0.70, "safe": 0.30, "unit": "ratio"},
    "earnings_cheapness": {"warn": 0.70, "safe": 0.30, "unit": "ratio"},
}


def _load_env() -> None:
    for p in (DATA_DIR / "config.env", DATA_DIR / ".env", Path.cwd() / "config.env", Path.cwd() / ".env"):
        if p.is_file():
            try:
                load_dotenv(p, override=True, encoding="utf-8-sig")
            except TypeError:
                load_dotenv(p, override=True)


def _database_url() -> str:
    _load_env()
    db = os.environ.get("DATABASE_URL", "").strip()
    if db:
        return db
    return f"sqlite:///{(DATA_DIR / 'market.db').as_posix()}"


def _cutoff_for_range(label: str) -> date | None:
    today = date.today()
    return {
        "1M": today - timedelta(days=31),
        "3M": today - timedelta(days=92),
        "6M": today - timedelta(days=183),
        "1Y": today - timedelta(days=366),
        "3Y": today - timedelta(days=366 * 3),
        "5Y": today - timedelta(days=366 * 5),
        "ALL": None,
    }[label]


def _load_valuation_scores(range_label: str) -> pd.DataFrame:
    cutoff = _cutoff_for_range(range_label)
    sql = (
        "select score_date, index_code, valuation_score, level_value, level_percentile_5y, "
        "macro_pressure_score, valuation_zone, components_json "
        "from macro_valuation_scores "
        + ("where score_date >= :cutoff " if cutoff else "")
        + "order by score_date, index_code"
    )
    engine = create_engine(_database_url(), future=True)
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"cutoff": cutoff} if cutoff else {}).fetchall()
    if not rows:
        return pd.DataFrame(
            columns=[
                "score_date",
                "index_code",
                "valuation_score",
                "level_value",
                "level_percentile_5y",
                "macro_pressure_score",
                "valuation_zone",
                "components_json",
            ]
        )
    df = pd.DataFrame(
        rows,
        columns=[
            "score_date",
            "index_code",
            "valuation_score",
            "level_value",
            "level_percentile_5y",
            "macro_pressure_score",
            "valuation_zone",
            "components_json",
        ],
    )
    df["score_date"] = pd.to_datetime(df["score_date"])
    df["components_json"] = df["components_json"].apply(
        lambda v: json.loads(v) if isinstance(v, str) and v.startswith("{") else (v if isinstance(v, dict) else {})
    )
    df["earnings_growth_percentile_5y"] = df["components_json"].apply(
        lambda d: float(d.get("earnings_growth_percentile_5y")) if isinstance(d, dict) and d.get("earnings_growth_percentile_5y") is not None else None
    )
    df["earnings_cheapness"] = 1.0 - df["earnings_growth_percentile_5y"]
    return df


def _factor_anomalies(series: pd.Series, window: int = 30, z_threshold: float = 2.5) -> pd.DataFrame:
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


def _factor_trend_chart(
    series: pd.Series,
    title: str,
    factor_key: str,
    guardrails: dict[str, dict[str, float | str]],
) -> None:
    s = series.dropna()
    if s.empty:
        st.caption(f"{title}: no data")
        return

    anomalies = _factor_anomalies(s)
    guard = guardrails.get(factor_key)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=s.index, y=s.values, mode="lines", name=title, line=dict(width=2)))

    if guard:
        fig.add_hline(y=guard["warn"], line_dash="dash", line_color="#d62728", annotation_text=f"Warn {guard['warn']}")
        fig.add_hline(y=guard["safe"], line_dash="dot", line_color="#2ca02c", annotation_text=f"Safe {guard['safe']}")

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
                    customdata=up[["delta", "z"]].to_numpy(),
                    hovertemplate="Date=%{x}<br>Value=%{y:.4f}<br>Delta=%{customdata[0]:.4f}<br>Z=%{customdata[1]:.2f}<extra></extra>",
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
                    customdata=down[["delta", "z"]].to_numpy(),
                    hovertemplate="Date=%{x}<br>Value=%{y:.4f}<br>Delta=%{customdata[0]:.4f}<br>Z=%{customdata[1]:.2f}<extra></extra>",
                )
            )

    fig.update_layout(title=title, height=330, margin=dict(l=10, r=10, t=45, b=20), legend_title="", xaxis_title="Date")
    st.plotly_chart(fig, use_container_width=True)
    if guard:
        st.caption(f"Guardrails ({guard['unit']}): red warn={guard['warn']}, green safe={guard['safe']}.")


def render() -> None:
    st.header("Macro Valuation Dashboard")
    st.caption("Source: macro_valuation_scores")

    range_label = st.radio("Time Range", ["1M", "3M", "6M", "1Y", "3Y", "5Y", "ALL"], index=3, horizontal=True, key="mv_time_range")
    df = _load_valuation_scores(range_label)
    if df.empty:
        st.warning("No rows found in macro_valuation_scores for this range.")
        return

    latest_by_index = df.sort_values("score_date").groupby("index_code", as_index=False).tail(1)
    cols = st.columns(max(1, len(latest_by_index)))
    for i, (_, r) in enumerate(latest_by_index.iterrows()):
        cols[i].metric(
            f"{r['index_code']} ({r['score_date'].date().isoformat()})",
            f"{float(r['valuation_score']):.2f}",
            delta=str(r["valuation_zone"]),
        )

    fig = px.line(df, x="score_date", y="valuation_score", color="index_code", title=f"Valuation Score Trend ({range_label})")
    fig.update_layout(yaxis_title="Valuation Score (0-100)", xaxis_title="Date", legend_title="")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Formula & Weights")
    st.markdown(
        "`valuation_score = 100 * (0.60*level_percentile_5y + 0.25*macro_pressure + 0.15*earnings_cheapness)`\n\n"
        "- `level_percentile_5y`: 指数点位 5 年分位\n"
        "- `macro_pressure`: 利率/信用/金融条件压力\n"
        "- `earnings_cheapness`: `1 - earnings_growth_percentile_5y`\n"
    )

    if not latest_by_index.empty:
        comps = latest_by_index.iloc[0]["components_json"] if isinstance(latest_by_index.iloc[0]["components_json"], dict) else {}
        weight_rows = [
            {"component": "level_percentile_5y", "weight": float(comps.get("weight_level", 0.60))},
            {"component": "macro_pressure", "weight": float(comps.get("weight_macro_pressure", 0.25))},
            {"component": "earnings_cheapness", "weight": float(comps.get("weight_earnings_cheapness", 0.15))},
        ]
        wdf = pd.DataFrame(weight_rows).sort_values("weight", ascending=False)
        c1, c2 = st.columns(2)
        c1.plotly_chart(px.pie(wdf, values="weight", names="component", title="Top-level Weights"), use_container_width=True)
        c2.plotly_chart(px.bar(wdf, x="component", y="weight", title="Weight by Component"), use_container_width=True)

    st.subheader("Latest Snapshot Detail")
    detail = latest_by_index[
        [
            "index_code",
            "score_date",
            "valuation_score",
            "valuation_zone",
            "level_value",
            "level_percentile_5y",
            "macro_pressure_score",
        ]
    ].sort_values("index_code")
    st.dataframe(detail, use_container_width=True)

    st.subheader("Valuation Factors - Trend View")
    st.caption("可在下方调整每个估值因子的安全线/告警线，图表会实时更新。")
    guardrails = {k: dict(v) for k, v in VAL_FACTOR_GUARDRAILS.items()}
    with st.expander("Adjust Guardrails", expanded=False):
        for factor_key, factor_title in VAL_FACTOR_PLOT_ORDER:
            defaults = guardrails.get(factor_key, {"warn": 0.7, "safe": 0.3, "unit": "ratio"})
            st.markdown(f"**{factor_title}**")
            c1, c2 = st.columns(2)
            warn_v = c1.slider(
                f"{factor_title} Warn",
                min_value=0.0,
                max_value=1.0,
                value=float(defaults["warn"]),
                step=0.01,
                key=f"mv_warn_{factor_key}",
            )
            safe_v = c2.slider(
                f"{factor_title} Safe",
                min_value=0.0,
                max_value=1.0,
                value=float(defaults["safe"]),
                step=0.01,
                key=f"mv_safe_{factor_key}",
            )
            guardrails[factor_key] = {"warn": warn_v, "safe": safe_v, "unit": str(defaults["unit"])}
            if safe_v >= warn_v:
                st.warning(f"{factor_title}: 建议 safe < warn，当前为 safe={safe_v:.2f}, warn={warn_v:.2f}")

    index_options = sorted(df["index_code"].dropna().unique().tolist())
    selected_index = st.radio(
        "Factor Chart Index",
        options=index_options,
        horizontal=True,
        key="mv_factor_index",
    )
    idx_df = (
        df[df["index_code"] == selected_index]
        .sort_values("score_date")
        .set_index("score_date")
    )
    for factor_col, title in VAL_FACTOR_PLOT_ORDER:
        if factor_col in idx_df.columns:
            _factor_trend_chart(
                idx_df[factor_col],
                f"{selected_index} - {title}",
                factor_col,
                guardrails,
            )

    with st.expander("Recent Rows"):
        show = df[
            [
                "score_date",
                "index_code",
                "valuation_score",
                "valuation_zone",
                "level_value",
                "level_percentile_5y",
                "macro_pressure_score",
                "earnings_cheapness",
            ]
        ].sort_values(["score_date", "index_code"]).tail(40)
        st.dataframe(show, use_container_width=True)
