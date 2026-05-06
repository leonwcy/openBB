"""Leon Platform homepage.

Run:
    streamlit run leon/platform/app.py
"""

from __future__ import annotations

import streamlit as st

from liquidity_page import render as render_liquidity
from macro_valuation_page import render as render_macro_valuation


def main() -> None:
    st.set_page_config(page_title="Leon Platform", layout="wide", initial_sidebar_state="collapsed")
    st.title("Leon Platform")
    st.caption("页面目录（不依赖侧边栏）")
    selected_page = st.radio(
        "选择页面",
        ["Liquidity Dashboard", "Macro Valuation Dashboard"],
        horizontal=True,
        label_visibility="collapsed",
    )
    st.info("Data source is your local database (configured by leon/data/config.env).")

    if selected_page == "Liquidity Dashboard":
        render_liquidity()
    else:
        render_macro_valuation()


if __name__ == "__main__":
    main()

