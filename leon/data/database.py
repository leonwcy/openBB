"""Database engine(s) and schema creation."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from models import (
    Base,
    DailyQuote,
    IngestionRun,
    MacroObservation,
    MacroSeriesCatalog,
    MacroSeriesState,
    PanicLiquidityScore,
    SymbolRegistry,
)
from partition_pg import ensure_partitioned_daily_quotes, ensure_partitioned_macro_observations

THIS_DIR = Path(__file__).resolve().parent


def default_sqlite_url() -> str:
    db_path = THIS_DIR / "market.db"
    return f"sqlite:///{db_path.as_posix()}"


def get_primary_database_url() -> str:
    return os.environ.get("DATABASE_URL", "").strip() or default_sqlite_url()


def _mirror_database_url() -> str | None:
    raw = os.environ.get("DATABASE_MIRROR_URL", "").strip()
    return raw or None


def make_engine(url: str):
    connect_args: dict = {}
    lowered = url.lower()
    if lowered.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        return create_engine(url, future=True, connect_args=connect_args)

    pool_size = int(os.environ.get("LEON_DB_POOL_SIZE", "24"))
    max_overflow = int(os.environ.get("LEON_DB_MAX_OVERFLOW", "48"))
    return create_engine(
        url,
        future=True,
        connect_args=connect_args,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
    )


def get_engines():
    """Ordered list of engines: primary, then optional mirror (deduped by URL)."""
    urls: list[str] = []
    primary = get_primary_database_url()
    urls.append(primary)
    mirror = _mirror_database_url()
    if mirror and mirror != primary:
        urls.append(mirror)
    return [make_engine(u) for u in urls]


def get_engine():
    """Single primary engine (backward compatible)."""
    return make_engine(get_primary_database_url())


def init_db(engines=None):
    """Create auxiliary tables and partitioned/non-partitioned fact tables by engine."""
    targets = engines or get_engines()
    auxiliary = [
        IngestionRun.__table__,
        SymbolRegistry.__table__,
        MacroSeriesCatalog.__table__,
        MacroSeriesState.__table__,
        PanicLiquidityScore.__table__,
    ]
    for eng in targets:
        Base.metadata.create_all(eng, tables=auxiliary)
        if eng.dialect.name == "postgresql":
            ensure_partitioned_daily_quotes(eng)
            ensure_partitioned_macro_observations(eng)
        else:
            Base.metadata.create_all(eng, tables=[DailyQuote.__table__, MacroObservation.__table__])
    return targets


@contextmanager
def session_scope(engine=None):
    eng = engine or get_engine()
    factory = sessionmaker(bind=eng, expire_on_commit=False, autoflush=False, future=True)
    session: Session = factory()
    try:
        yield session
    finally:
        session.close()


def engine_label(engine) -> str:
    try:
        return engine.url.render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        return str(engine)


