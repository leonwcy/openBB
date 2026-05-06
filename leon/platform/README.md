# leon/platform

单页目录导航 Streamlit UI（流动性 + 宏观估值）。

## 文件

- `app.py`：主页入口（页面目录切换，不依赖侧边栏）
- `liquidity_page.py`：流动性分数 + PIZZA 指数渲染模块
- `macro_valuation_page.py`：宏观估值分数（SP500 / NASDAQ）渲染模块

## 功能

- 展示 `liquidity_score`（并同时展示 `panic_score` 作为对照）
- 时间范围切换：`1M / 3M / 6M / 1Y / 3Y / 5Y / ALL`
- 最新值卡片 + 折线图 + 因子趋势图 + 最近数据表
- 展示 `macro_valuation_scores`（SP500/NASDAQ）的估值分数与分区
- 顶部“页面目录”横向切换：`Liquidity Dashboard / Macro Valuation Dashboard`

## 运行

1) 安装依赖

```bash
pip install -r leon/platform/requirements.txt
```

2) 启动 UI

```bash
streamlit run leon/platform/app.py
```

## 数据源

- 表：`panic_liquidity_scores`、`macro_valuation_scores`、`macro_observations`
- 连接：优先读取 `leon/data/config.env` 中的 `DATABASE_URL`
- 若未配置 `DATABASE_URL`，默认回退到 `leon/data/market.db`（SQLite）

