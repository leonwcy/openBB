# leon/platform

流动性分数 UI（基于 `panic_liquidity_scores`）。

## 功能

- 展示 `liquidity_score`（并同时展示 `panic_score` 作为对照）
- 时间范围切换：`1M / 3M / 6M / 1Y / 3Y / 5Y / ALL`
- 最新值卡片 + 折线图 + 最近数据表

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

- 表：`panic_liquidity_scores`
- 连接：优先读取 `leon/data/config.env` 中的 `DATABASE_URL`
- 若未配置 `DATABASE_URL`，默认回退到 `leon/data/market.db`（SQLite）

