"""Persistence helpers for macro tables."""

from __future__ import annotations

from datetime import date, datetime, timezone

from models import MacroObservation, MacroSeriesState
from sqlalchemy import select


def upsert_macro_observation(
    session,
    *,
    provider: str,
    series_id: str,
    observation_date: date,
    value: float | None,
    value_text: str | None,
    vintage_date: date,
    released_at: datetime | None,
) -> None:
    existing = session.execute(
        select(MacroObservation).where(
            MacroObservation.provider == provider,
            MacroObservation.series_id == series_id,
            MacroObservation.observation_date == observation_date,
            MacroObservation.vintage_date == vintage_date,
        )
    ).scalar_one_or_none()
    if existing:
        existing.value = value
        existing.value_text = value_text
        existing.released_at = released_at
        existing.ingested_at = datetime.now(timezone.utc).replace(tzinfo=None)
    else:
        session.add(
            MacroObservation(
                provider=provider,
                series_id=series_id,
                observation_date=observation_date,
                vintage_date=vintage_date,
                value=value,
                value_text=value_text,
                released_at=released_at,
                ingested_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )


def upsert_macro_series_state(
    session,
    *,
    provider: str,
    series_id: str,
    last_observation_date: date | None,
    success: bool,
    error: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    state = session.execute(
        select(MacroSeriesState).where(
            MacroSeriesState.provider == provider,
            MacroSeriesState.series_id == series_id,
        )
    ).scalar_one_or_none()
    if state is None:
        session.add(
            MacroSeriesState(
                provider=provider,
                series_id=series_id,
                last_observation_date=last_observation_date,
                last_vintage_date=date.today() if success else None,
                last_success_at=now if success else None,
                last_error=error if not success else None,
                updated_at=now,
            )
        )
        return

    if success:
        state.last_observation_date = last_observation_date
        state.last_vintage_date = date.today()
        state.last_success_at = now
        state.last_error = None
    else:
        state.last_error = error
    state.updated_at = now

