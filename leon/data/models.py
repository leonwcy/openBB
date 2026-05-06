"""SQLAlchemy models for Leon market + macro data landing."""

from datetime import date, datetime

from sqlalchemy import JSON, BigInteger, Date, DateTime, Float, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base."""


class IngestionRun(Base):
    """One row per daily job execution."""

    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text)
    exchanges: Mapped[str | None] = mapped_column(Text)
    rows_inserted: Mapped[int] = mapped_column(default=0)


class DailyQuote(Base):
    """End-of-day row (snapshot or historical backfill).

    Composite primary key (symbol, exchange, trade_date) is required for
    PostgreSQL RANGE partitioning on trade_date.
    """

    __tablename__ = "daily_quotes"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(32), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date(), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(512))
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    prev_close: Mapped[float | None] = mapped_column(Float)
    change_percent: Mapped[float | None] = mapped_column(Float)
    market_cap: Mapped[float | None] = mapped_column(Float)
    ma50: Mapped[float | None] = mapped_column(Float)
    ma200: Mapped[float | None] = mapped_column(Float)
    year_high: Mapped[float | None] = mapped_column(Float)
    year_low: Mapped[float | None] = mapped_column(Float)
    quote_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=False))

    __table_args__ = (Index("ix_daily_trade_date_symbol", "trade_date", "symbol"),)


class SymbolRegistry(Base):
    """Symbols seen across runs (helps quarterly universe / survivorship work later)."""

    __tablename__ = "symbol_registry"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str | None] = mapped_column(String(512))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=False))

    __table_args__ = (UniqueConstraint("symbol", "exchange", name="uq_registry_symbol_exchange"),)


class MacroSeriesCatalog(Base):
    """Catalog of macro series to ingest."""

    __tablename__ = "macro_series_catalog"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    series_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    series_name: Mapped[str | None] = mapped_column(String(256))
    category: Mapped[str] = mapped_column(String(64))
    frequency: Mapped[str | None] = mapped_column(String(16))
    units: Mapped[str | None] = mapped_column(String(64))
    country: Mapped[str] = mapped_column(String(32), default="US")
    route: Mapped[str] = mapped_column(String(128), default="economy.fred_series")
    params_json: Mapped[dict | None] = mapped_column(JSON)
    priority_tier: Mapped[int] = mapped_column(default=1)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_macro_catalog_active_priority", "is_active", "priority_tier"),
        Index("ix_macro_catalog_category", "category"),
    )


class MacroObservation(Base):
    """Macro observation values by provider and series."""

    __tablename__ = "macro_observations"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    series_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    observation_date: Mapped[date] = mapped_column(Date(), primary_key=True)
    vintage_date: Mapped[date] = mapped_column(Date(), primary_key=True, default=date.today)
    value: Mapped[float | None] = mapped_column(Float)
    value_text: Mapped[str | None] = mapped_column(String(64))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_macro_obs_series_date", "provider", "series_id", "observation_date"),
        Index("ix_macro_obs_date", "observation_date"),
    )


class MacroSeriesState(Base):
    """Incremental checkpoints for macro series ingestion."""

    __tablename__ = "macro_series_state"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    series_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_observation_date: Mapped[date | None] = mapped_column(Date())
    last_vintage_date: Mapped[date | None] = mapped_column(Date())
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class PanicLiquidityScore(Base):
    """Derived market panic/liquidity score time series."""

    __tablename__ = "panic_liquidity_scores"

    score_date: Mapped[date] = mapped_column(Date(), primary_key=True)
    panic_score: Mapped[float] = mapped_column(Float)  # 0-100
    liquidity_score: Mapped[float] = mapped_column(Float)  # 0-100
    regime: Mapped[str] = mapped_column(String(32))
    components_json: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_panic_score_date", "score_date"),
        Index("ix_panic_regime", "regime"),
    )


class MacroValuationScore(Base):
    """Derived macro valuation scores for major US indices."""

    __tablename__ = "macro_valuation_scores"

    score_date: Mapped[date] = mapped_column(Date(), primary_key=True)
    index_code: Mapped[str] = mapped_column(String(32), primary_key=True)  # SP500 / NASDAQCOM
    valuation_score: Mapped[float] = mapped_column(Float)  # 0-100, higher = more expensive/richer
    level_value: Mapped[float | None] = mapped_column(Float)
    level_percentile_5y: Mapped[float | None] = mapped_column(Float)  # 0-1
    macro_pressure_score: Mapped[float | None] = mapped_column(Float)  # 0-1
    valuation_zone: Mapped[str] = mapped_column(String(32))  # very_low / low / neutral / high / very_high
    components_json: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_macro_valuation_date", "score_date"),
        Index("ix_macro_valuation_index", "index_code"),
        Index("ix_macro_valuation_zone", "valuation_zone"),
    )
