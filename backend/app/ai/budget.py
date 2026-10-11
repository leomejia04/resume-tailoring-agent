"""Atomic spending limits: reserve a worst-case cost before a call, settle after.

Each reservation is one conditional UPDATE (``spent + reserved + amount <= cap``),
so concurrent requests serialize on the row lock and cannot jointly overspend.
Per-user counters are protected by row-level security; the app-wide counter
holds aggregate amounts only.

Every reservation is also a row with an expiry. Settling or releasing deletes
the row and subtracts its amount only if the delete found it, so a crash
between reserve and settle cannot hold budget forever: the worker returns
expired rows (``app.release_expired_reservations``), and a late settle after
that never subtracts twice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ai.pricing import Usage
from app.config import Settings
from app.db.models import UsageEvent


class BudgetExceededError(Exception):
    def __init__(self, scope: str) -> None:
        super().__init__(scope)
        self.scope = scope  # "user" or "app"


@dataclass(frozen=True)
class Reservation:
    id: uuid.UUID
    day: date
    amount: Decimal


def today() -> date:
    return datetime.now(UTC).date()


_ENSURE_USER = text(
    "INSERT INTO app.spend_user (owner_id, day) VALUES (:owner, :day) ON CONFLICT DO NOTHING"
)
_RESERVE_USER = text(
    """
    UPDATE app.spend_user SET reserved_usd = reserved_usd + :amount
    WHERE owner_id = :owner AND day = :day AND spent_usd + reserved_usd + :amount <= :cap
    RETURNING 1
    """
)
_ENSURE_APP = text("INSERT INTO app.spend_global (day) VALUES (:day) ON CONFLICT DO NOTHING")
_RESERVE_APP = text(
    """
    UPDATE app.spend_global SET reserved_usd = reserved_usd + :amount
    WHERE day = :day AND spent_usd + reserved_usd + :amount <= :cap
    RETURNING 1
    """
)
_RECORD = text(
    """
    INSERT INTO app.budget_reservations (id, owner_id, day, amount_usd, expires_at)
    VALUES (:id, :owner, :day, :amount, :expires)
    """
)
# One statement: delete the reservation row, then move only what was really
# deleted out of "reserved" (zero if the expiry sweep already returned it).
_SETTLE = text(
    """
    WITH gone AS (
      DELETE FROM app.budget_reservations WHERE id = :id AND owner_id = :owner
      RETURNING amount_usd
    ),
    amount AS (SELECT coalesce(sum(amount_usd), 0) AS v FROM gone),
    by_user AS (
      UPDATE app.spend_user
      SET reserved_usd = GREATEST(reserved_usd - (SELECT v FROM amount), 0),
          spent_usd = spent_usd + :actual
      WHERE owner_id = :owner AND day = :day
      RETURNING 1
    )
    UPDATE app.spend_global
    SET reserved_usd = GREATEST(reserved_usd - (SELECT v FROM amount), 0),
        spent_usd = spent_usd + :actual
    WHERE day = :day
    """
)


def reserve(db: Session, settings: Settings, owner_id: uuid.UUID, amount: Decimal) -> Reservation:
    """Reserve inside the caller's transaction; commit it before the slow model call."""
    day = today()
    params = {"owner": owner_id, "day": day, "amount": amount}
    db.execute(_ENSURE_USER, params)
    if db.execute(_RESERVE_USER, {**params, "cap": settings.user_daily_budget_usd}).first() is None:
        raise BudgetExceededError("user")
    db.execute(_ENSURE_APP, params)
    if db.execute(_RESERVE_APP, {**params, "cap": settings.app_daily_budget_usd}).first() is None:
        raise BudgetExceededError("app")
    reservation = Reservation(id=uuid.uuid4(), day=day, amount=amount)
    expires = datetime.now(UTC) + timedelta(seconds=settings.reservation_ttl_seconds)
    db.execute(_RECORD, {**params, "id": reservation.id, "expires": expires})
    return reservation


def _close(db: Session, owner_id: uuid.UUID, reservation: Reservation, actual: Decimal) -> None:
    db.execute(
        _SETTLE,
        {"id": reservation.id, "owner": owner_id, "day": reservation.day, "actual": actual},
    )


def release(db: Session, owner_id: uuid.UUID, reservation: Reservation) -> None:
    """Return a reservation that was never used (no model call, so no usage event)."""
    _close(db, owner_id, reservation, Decimal(0))


def settle(
    db: Session,
    owner_id: uuid.UUID,
    reservation: Reservation,
    actual: Decimal,
    *,
    usage: Usage,
    run_id: uuid.UUID | None,
    operation: str,
    model: str,
    prompt_version: str,
    latency_ms: int,
    outcome: str,
) -> None:
    """Replace the reservation with the actual cost and record the usage event."""
    _close(db, owner_id, reservation, actual)
    db.add(
        UsageEvent(
            owner_id=owner_id,
            run_id=run_id,
            operation=operation,
            model=model,
            prompt_version=prompt_version,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cost_usd=actual,
            latency_ms=latency_ms,
            outcome=outcome,
        )
    )
