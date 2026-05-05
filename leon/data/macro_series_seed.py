"""Seed and maintain the macro series catalog."""

from __future__ import annotations

from datetime import datetime, timezone

from database import get_engines, init_db, session_scope
from models import MacroSeriesCatalog
from sqlalchemy import select

# Prioritized US macro panel for equity market analysis.
DEFAULT_MACRO_SERIES = [
    # Alternative / geopolitical proxy
    {"provider": "alt", "series_id": "PIZZA", "series_name": "Nothing Ever Happens Index (NEH)", "category": "fear", "frequency": "daily", "priority_tier": 2},
    # Panic / fear
    {"provider": "fred", "series_id": "VIXCLS", "series_name": "CBOE Volatility Index (VIX)", "category": "fear", "frequency": "daily", "priority_tier": 1},
    {"provider": "fred", "series_id": "TEDRATE", "series_name": "TED Spread", "category": "liquidity", "frequency": "daily", "priority_tier": 1},
    # Rates / curve
    {"provider": "fred", "series_id": "DFF", "series_name": "Effective Federal Funds Rate", "category": "rates", "frequency": "daily", "priority_tier": 1},
    {"provider": "fred", "series_id": "SOFR", "series_name": "Secured Overnight Financing Rate", "category": "rates", "frequency": "daily", "priority_tier": 1},
    {"provider": "fred", "series_id": "DGS2", "series_name": "2-Year Treasury Constant Maturity Rate", "category": "rates", "frequency": "daily", "priority_tier": 1},
    {"provider": "fred", "series_id": "DGS10", "series_name": "10-Year Treasury Constant Maturity Rate", "category": "rates", "frequency": "daily", "priority_tier": 1},
    {"provider": "fred", "series_id": "T10Y2Y", "series_name": "10Y-2Y Treasury Spread", "category": "rates", "frequency": "daily", "priority_tier": 1},
    # Inflation
    {"provider": "fred", "series_id": "CPIAUCSL", "series_name": "CPI All Urban Consumers", "category": "inflation", "frequency": "monthly", "priority_tier": 1},
    {"provider": "fred", "series_id": "CPILFESL", "series_name": "Core CPI", "category": "inflation", "frequency": "monthly", "priority_tier": 1},
    {"provider": "fred", "series_id": "PCEPI", "series_name": "PCE Price Index", "category": "inflation", "frequency": "monthly", "priority_tier": 1},
    {"provider": "fred", "series_id": "PCEPILFE", "series_name": "Core PCE Price Index", "category": "inflation", "frequency": "monthly", "priority_tier": 1},
    # Labor
    {"provider": "fred", "series_id": "UNRATE", "series_name": "Unemployment Rate", "category": "labor", "frequency": "monthly", "priority_tier": 1},
    {"provider": "fred", "series_id": "PAYEMS", "series_name": "Total Nonfarm Payrolls", "category": "labor", "frequency": "monthly", "priority_tier": 1},
    {"provider": "fred", "series_id": "ICSA", "series_name": "Initial Claims", "category": "labor", "frequency": "weekly", "priority_tier": 1},
    # Growth
    {"provider": "fred", "series_id": "GDPC1", "series_name": "Real GDP", "category": "growth", "frequency": "quarterly", "priority_tier": 1},
    {"provider": "fred", "series_id": "INDPRO", "series_name": "Industrial Production Index", "category": "growth", "frequency": "monthly", "priority_tier": 1},
    {"provider": "fred", "series_id": "RSAFS", "series_name": "Retail Sales", "category": "growth", "frequency": "monthly", "priority_tier": 1},
    {"provider": "fred", "series_id": "NAPM", "series_name": "ISM Manufacturing PMI", "category": "growth", "frequency": "monthly", "priority_tier": 1},
    # Credit / liquidity
    {"provider": "fred", "series_id": "NFCI", "series_name": "National Financial Conditions Index", "category": "credit", "frequency": "weekly", "priority_tier": 1},
    {"provider": "fred", "series_id": "BAMLH0A0HYM2", "series_name": "US High Yield OAS", "category": "credit", "frequency": "daily", "priority_tier": 1},
    {"provider": "fred", "series_id": "BAMLC0A0CM", "series_name": "US Corporate Master OAS", "category": "credit", "frequency": "daily", "priority_tier": 1},
    {"provider": "fred", "series_id": "M2SL", "series_name": "M2 Money Stock", "category": "liquidity", "frequency": "monthly", "priority_tier": 1},
]


def seed_macro_catalog() -> None:
    engines = init_db()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for eng in engines:
        with session_scope(eng) as session:
            for item in DEFAULT_MACRO_SERIES:
                provider = item["provider"]
                series_id = item["series_id"]
                row = session.execute(
                    select(MacroSeriesCatalog).where(
                        MacroSeriesCatalog.provider == provider,
                        MacroSeriesCatalog.series_id == series_id,
                    )
                ).scalar_one_or_none()
                if row:
                    row.series_name = item.get("series_name")
                    row.category = item.get("category", row.category)
                    row.frequency = item.get("frequency", row.frequency)
                    row.priority_tier = int(item.get("priority_tier", row.priority_tier))
                    row.route = "alt.custom_series" if provider == "alt" else "economy.fred_series"
                    row.is_active = True
                    row.updated_at = now
                else:
                    session.add(
                        MacroSeriesCatalog(
                            provider=provider,
                            series_id=series_id,
                            series_name=item.get("series_name"),
                            category=item.get("category", "macro"),
                            frequency=item.get("frequency"),
                            units=None,
                            country="US",
                            route="alt.custom_series" if provider == "alt" else "economy.fred_series",
                            params_json=None,
                            priority_tier=int(item.get("priority_tier", 1)),
                            is_active=True,
                            created_at=now,
                            updated_at=now,
                        )
                    )
            session.commit()
            # Disable deprecated alternative series so incremental/full won't use it.
            deprecated = session.execute(
                select(MacroSeriesCatalog).where(
                    MacroSeriesCatalog.provider == "alt",
                    MacroSeriesCatalog.series_id == "PENTAGON_PIZZA_INDEX",
                )
            ).scalar_one_or_none()
            if deprecated and deprecated.is_active:
                deprecated.is_active = False
                deprecated.updated_at = now
                session.commit()


if __name__ == "__main__":
    seed_macro_catalog()
    print(f"Macro catalog seeded with {len(DEFAULT_MACRO_SERIES)} default series.")

