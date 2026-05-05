"""PostgreSQL partition DDL helpers (daily_quotes + macro_observations)."""

from __future__ import annotations

import os
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Engine

# Matches models.DailyQuote (composite PK; no surrogate id).
_DAILY_QUOTES_PARTITIONED_TABLE_DDL = (
    "symbol VARCHAR(32) NOT NULL, "
    "exchange VARCHAR(32) NOT NULL, "
    "trade_date DATE NOT NULL, "
    "name VARCHAR(512), "
    "open DOUBLE PRECISION, "
    "high DOUBLE PRECISION, "
    "low DOUBLE PRECISION, "
    "close DOUBLE PRECISION, "
    "volume BIGINT, "
    "prev_close DOUBLE PRECISION, "
    "change_percent DOUBLE PRECISION, "
    "market_cap DOUBLE PRECISION, "
    "ma50 DOUBLE PRECISION, "
    "ma200 DOUBLE PRECISION, "
    "year_high DOUBLE PRECISION, "
    "year_low DOUBLE PRECISION, "
    "quote_timestamp TIMESTAMP WITHOUT TIME ZONE, "
    "ingested_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, "
    "PRIMARY KEY (symbol, exchange, trade_date)"
)

_MACRO_OBS_PARTITIONED_TABLE_DDL = (
    "provider VARCHAR(32) NOT NULL, "
    "series_id VARCHAR(128) NOT NULL, "
    "observation_date DATE NOT NULL, "
    "vintage_date DATE NOT NULL DEFAULT CURRENT_DATE, "
    "value DOUBLE PRECISION, "
    "value_text VARCHAR(64), "
    "released_at TIMESTAMP WITHOUT TIME ZONE, "
    "ingested_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, "
    "PRIMARY KEY (provider, series_id, observation_date, vintage_date)"
)


def _is_postgres(engine: Engine) -> bool:
    return engine.dialect.name == "postgresql"


def _table_relkind(engine: Engine, table_name: str) -> str | None:
    """Return pg relkind: 'r' heap, 'p' partitioned table, None if missing."""
    sql = text(
        """
        SELECT c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = :table_name
        """
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {"table_name": table_name}).first()
        if row is None:
            return None
        return row[0]


def _partition_year_bounds() -> tuple[int, int]:
    y = date.today().year
    year_from = int(os.environ.get("LEON_PARTITION_YEAR_FROM", str(y - 6)))
    year_to = int(os.environ.get("LEON_PARTITION_YEAR_TO", str(y + 2)))
    return year_from, year_to


def _create_child_partitions(conn, base_table: str, child_prefix: str, y0: int, y1: int, key: str) -> None:
    for y in range(y0, y1 + 1):
        child = f"{child_prefix}{y}"
        conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {child} PARTITION OF {base_table} "
                f"FOR VALUES FROM ('{y}-01-01') TO ('{y + 1}-01-01')"
            )
        )


def ensure_year_partitions(engine: Engine) -> None:
    """Attach missing yearly partitions for an existing partitioned parent."""
    if not _is_postgres(engine):
        return
    if _table_relkind(engine, "daily_quotes") != "p":
        return
    y0, y1 = _partition_year_bounds()
    with engine.begin() as conn:
        _create_child_partitions(conn, "daily_quotes", "daily_quotes_y", y0, y1, "trade_date")


def _create_fresh_partitioned_table(engine: Engine) -> None:
    """New DB: parent + yearly partitions + index (never creates a heap daily_quotes)."""
    y0, y1 = _partition_year_bounds()
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE daily_quotes ({_DAILY_QUOTES_PARTITIONED_TABLE_DDL}) "
                "PARTITION BY RANGE (trade_date)"
            )
        )
        _create_child_partitions(conn, "daily_quotes", "daily_quotes_y", y0, y1, "trade_date")
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_daily_trade_date_symbol "
                "ON daily_quotes (trade_date, symbol)"
            )
        )


def _migrate_heap_to_partitioned(engine: Engine) -> None:
    """Legacy path: ORM created heap daily_quotes — rename, rebuild partitioned, copy rows."""
    y0, y1 = _partition_year_bounds()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE daily_quotes RENAME TO daily_quotes_heap_legacy"))
        conn.execute(
            text(
                f"CREATE TABLE daily_quotes ({_DAILY_QUOTES_PARTITIONED_TABLE_DDL}) "
                "PARTITION BY RANGE (trade_date)"
            )
        )
        _create_child_partitions(conn, "daily_quotes", "daily_quotes_y", y0, y1, "trade_date")
        conn.execute(
            text(
                """
                INSERT INTO daily_quotes (
                    symbol, exchange, trade_date, name, open, high, low, close, volume,
                    prev_close, change_percent, market_cap, ma50, ma200, year_high, year_low,
                    quote_timestamp, ingested_at
                )
                SELECT
                    symbol, exchange, trade_date, name, open, high, low, close, volume,
                    prev_close, change_percent, market_cap, ma50, ma200, year_high, year_low,
                    quote_timestamp, ingested_at
                FROM daily_quotes_heap_legacy
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_daily_trade_date_symbol "
                "ON daily_quotes (trade_date, symbol)"
            )
        )
        conn.execute(text("DROP TABLE daily_quotes_heap_legacy"))


def ensure_partitioned_daily_quotes(engine: Engine) -> None:
    """Public entry: guarantee PostgreSQL daily_quotes is partitioned by trade_date (year children).

    - Missing table -> CREATE partitioned parent + children.
    - Heap table (legacy ORM) -> migrate in-place.
    - Already partitioned -> CREATE IF NOT EXISTS any missing year partitions.
    """
    if not _is_postgres(engine):
        return
    kind = _table_relkind(engine, "daily_quotes")
    if kind == "p":
        ensure_year_partitions(engine)
        return
    if kind == "r":
        _migrate_heap_to_partitioned(engine)
        ensure_year_partitions(engine)
        return
    if kind is None:
        _create_fresh_partitioned_table(engine)
        return


def ensure_partitioned_macro_observations(engine: Engine) -> None:
    """Guarantee macro_observations is RANGE(observation_date) with yearly partitions."""
    if not _is_postgres(engine):
        return
    kind = _table_relkind(engine, "macro_observations")
    y0, y1 = _partition_year_bounds()

    if kind == "p":
        with engine.begin() as conn:
            _create_child_partitions(
                conn,
                "macro_observations",
                "macro_observations_y",
                y0,
                y1,
                "observation_date",
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_macro_obs_series_date "
                    "ON macro_observations (provider, series_id, observation_date)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_macro_obs_date "
                    "ON macro_observations (observation_date)"
                )
            )
        return

    if kind is None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"CREATE TABLE macro_observations ({_MACRO_OBS_PARTITIONED_TABLE_DDL}) "
                    "PARTITION BY RANGE (observation_date)"
                )
            )
            _create_child_partitions(
                conn,
                "macro_observations",
                "macro_observations_y",
                y0,
                y1,
                "observation_date",
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_macro_obs_series_date "
                    "ON macro_observations (provider, series_id, observation_date)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_macro_obs_date "
                    "ON macro_observations (observation_date)"
                )
            )
        return

    if kind == "r":
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE macro_observations RENAME TO macro_observations_heap_legacy"))
            conn.execute(
                text(
                    f"CREATE TABLE macro_observations ({_MACRO_OBS_PARTITIONED_TABLE_DDL}) "
                    "PARTITION BY RANGE (observation_date)"
                )
            )
            _create_child_partitions(
                conn,
                "macro_observations",
                "macro_observations_y",
                y0,
                y1,
                "observation_date",
            )
            conn.execute(
                text(
                    """
                    INSERT INTO macro_observations (
                      provider, series_id, observation_date, vintage_date, value, value_text,
                      released_at, ingested_at
                    )
                    SELECT
                      provider, series_id, observation_date, vintage_date, value, value_text,
                      released_at, ingested_at
                    FROM macro_observations_heap_legacy
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_macro_obs_series_date "
                    "ON macro_observations (provider, series_id, observation_date)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_macro_obs_date "
                    "ON macro_observations (observation_date)"
                )
            )
            conn.execute(text("DROP TABLE macro_observations_heap_legacy"))
        return


# Backwards-compatible name used earlier in database.py
def upgrade_daily_quotes_to_partitioned(engine: Engine) -> None:
    """Deprecated alias; prefer ensure_partitioned_daily_quotes."""
    ensure_partitioned_daily_quotes(engine)
