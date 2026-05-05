"""Shared upsert helpers for daily_quotes and symbol_registry."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select


def upsert_daily_quote(session, row: dict) -> None:
    from models import DailyQuote

    existing = session.execute(
        select(DailyQuote).where(
            DailyQuote.symbol == row["symbol"],
            DailyQuote.exchange == row["exchange"],
            DailyQuote.trade_date == row["trade_date"],
        )
    ).scalar_one_or_none()
    if existing:
        for key in (
            "name",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "prev_close",
            "change_percent",
            "market_cap",
            "ma50",
            "ma200",
            "year_high",
            "year_low",
            "quote_timestamp",
            "ingested_at",
        ):
            setattr(existing, key, row[key])
    else:
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
