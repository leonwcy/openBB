# leon/data 使用说明

这个目录是你的本地数据落库与调度层，目标是：

- 美股行情：全市场日度快照 + 5 年历史回填 + 日常增量
- 宏观数据：核心系列全量回填 + 日常增量
- 衍生指标：恐慌/流动性合成评分（0-100）

数据库优先使用 PostgreSQL（可选镜像到 SQLite）。

---

## 1. 文件作用总览

### 配置与依赖

- `config.example.env`：环境变量模板（复制为 `config.env` 使用）
- `config.env`：你本机真实配置（密钥、库连接、并发参数）
- `requirements.txt`：运行脚本依赖
- `.gitignore`：忽略本地私有配置、数据库文件、进度文件

### 数据库结构与分区

- `models.py`：SQLAlchemy 数据模型（行情、宏观、评分、任务运行记录）
- `database.py`：引擎创建、连接池、`init_db()` 初始化入口
- `partition_pg.py`：PostgreSQL 年分区逻辑（`daily_quotes`、`macro_observations`）

### 股票数据脚本

- `ingest_daily_snapshot.py`：全市场日度快照入库（FMP market snapshots）
- `ingest_eod_backfill.py`：按 symbol 回填历史日线（默认 FMP，可并发、断点续跑）
- `quotes_repo.py`：股票表 upsert 复用逻辑
- `run_daily.bat`：Windows 定时任务入口（日度快照）

### 宏观数据脚本

- `macro_series_seed.py`：写入/更新宏观系列清单（`macro_series_catalog`）
- `ingest_macro_full.py`：宏观全量回填（默认 5 年）
- `ingest_macro_incremental.py`：宏观增量更新（带 bootstrap 回退能力）
- `macro_repo.py`：宏观表 upsert 与 state 更新逻辑
- `run_macro_full.bat`：宏观全量任务入口
- `run_macro_incremental.bat`：宏观增量任务入口

### 指标构建

- `build_panic_liquidity_score.py`：构建恐慌/流动性评分并写入 `panic_liquidity_scores`

### 另类数据（NEH 指数）

- `alt_pizza_source.py`：从 API/JSON 读取 `NOTHING_EVER_HAPPENS_INDEX`（不使用 CSV 文件），并通过宏观全量/增量脚本落库到 PostgreSQL
- `check_pizza_source.py`：URL 连通性与字段预检查（跑全量前先验 API 结构）
  - 若 URL 不是 JSON，会自动尝试 fallback：从 `LEON_PIZZA_FALLBACK_URL` 页面提取 `DOUGHCON` 生成当日值

### 运行产物（本地）

- `market.db`：SQLite 本地库（如果启用 SQLite 或镜像）
- `backfill_progress.json`：历史回填进度（断点续跑）

---

## 2. 首次初始化（建议顺序）

1) 复制配置文件

```bash
copy leon\data\config.example.env leon\data\config.env
```

2) 修改 `config.env`（至少要改）

- `DATABASE_URL=postgresql+psycopg://...`
- `FMP_API_KEY=...`
- （建议）`FRED_API_KEY=...`

3) 安装依赖

```bash
pip install -r leon/data/requirements.txt
```

4) 初始化宏观清单（会自动建表）

```bash
python leon/data/macro_series_seed.py
```

---

## 3. 全量数据操作步骤

### A) 股票全量（历史 5 年）

先确保你已跑过一次快照（用于生成 `symbol_registry`）：

```bash
python leon/data/ingest_daily_snapshot.py
```

再跑历史回填：

```bash
python leon/data/ingest_eod_backfill.py
```

关键参数（`config.env`）：

- `LEON_EOD_BACKFILL_YEARS=5`
- `LEON_EOD_PROVIDER=fmp`
- `LEON_BACKFILL_WORKERS=16`
- `LEON_BACKFILL_LIMIT=0`（全量）
- `LEON_BACKFILL_RESUME=1`
- `LEON_BACKFILL_PROGRESS_EVERY=1000`
- `LEON_BACKFILL_CLEAR_PROGRESS_ON_COMPLETE=1`

### B) 宏观全量（历史 5 年）

```bash
python leon/data/macro_series_seed.py
python leon/data/check_pizza_source.py
python leon/data/ingest_macro_full.py
python leon/data/build_panic_liquidity_score.py
```

关键参数（`config.env`）：

- `LEON_MACRO_FULL_YEARS=5`
- `LEON_MACRO_MAX_PRIORITY=3`
- `LEON_MACRO_WORKERS=8`

---

## 4. 增量数据操作步骤（日常）

### A) 股票增量（日快照）

```bash
python leon/data/ingest_daily_snapshot.py
```

### B) 宏观增量

```bash
python leon/data/ingest_macro_incremental.py
python leon/data/build_panic_liquidity_score.py
```

说明：`ingest_macro_incremental.py` 在没有 state 时会自动按 `LEON_MACRO_BOOTSTRAP_YEARS` 回退补数，不会只做“空增量”。

---

## 5. Windows 任务计划建议

### 每日

- `run_daily.bat`：每天 07:00（UTC+8）
- `run_macro_incremental.bat`：每天 07:30（UTC+8）
- 可再加一个任务执行：
  - `python leon/data/build_panic_liquidity_score.py`（例如 07:40）

### 每周

- 周六 09:00（UTC+8）再跑一次：
  - `run_macro_incremental.bat`
  - 可选重建评分脚本

### 全量任务

- `run_macro_full.bat` 与 `ingest_eod_backfill.py` 不建议每日定时
- 建议首次上线、策略重构或季度维护时手动执行

---

## 6. 常见问题

- 只跑了 200 个 symbol：检查 `LEON_BACKFILL_LIMIT`，全量应为 `0`
- FMP key 报未设置：确认写在 `config.env`（不是 `config.example.env`）
- PostgreSQL 分区看不到：确认 `DATABASE_URL` 指向 PostgreSQL；`init_db()` 会自动创建/迁移分区表
- Pizza 指数无数据：确认 `LEON_PIZZA_SOURCE_URL` 可访问，且 JSON 字段名与配置中的 `LEON_PIZZA_JSON_*` 对齐

