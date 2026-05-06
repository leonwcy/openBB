"""Shared write helpers for daily_quotes and symbol_registry."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


def upsert_daily_quote(session, row: dict) -> None:
    from models import DailyQuote

    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        stmt = pg_insert(DailyQuote).values(**row).on_conflict_do_nothing(
            index_elements=["symbol", "exchange", "trade_date"]
        )
        session.execute(stmt)
        return
    if dialect == "sqlite":
        stmt = sqlite_insert(DailyQuote).values(**row).on_conflict_do_nothing(
            index_elements=["symbol", "exchange", "trade_date"]
        )
        session.execute(stmt)
        return

    # Generic fallback for other DBs
    existing = session.execute(
        select(DailyQuote).where(
            DailyQuote.symbol == row["symbol"],
            DailyQuote.exchange == row["exchange"],
            DailyQuote.trade_date == row["trade_date"],
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(DailyQuote(**row))


def touch_registry(session, row: dict, utc_now: datetime) -> None:
    from models import SymbolRegistry

    sym, ex_ch = row["symbol"], row["exchange"]
    existing = session.execute(
        select(SymbolRegistry).where(
            SymbolRegistry.symbol == sym,
            SymbolRegistry.exchange == ex_ch,
        )
    ).scalar_one_or_none()
    if existing:
        existing.last_seen_at = utc_now
        if row.get("name"):
            existing.name = row["name"]
    else:
        session.add(
            SymbolRegistry(
                symbol=sym,
                exchange=ex_ch,
                name=row.get("name"),
                first_seen_at=utc_now,
                last_seen_at=utc_now,
            )
        )
