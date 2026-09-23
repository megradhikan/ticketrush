"""
Postgres-only hold/release/expiry (PRD 5.2, strategy 1). This is the durable,
source-of-truth path: every transition here goes through
app.core.state_machine.assert_legal_transition, and every write is guarded by
`SELECT ... FOR UPDATE SKIP LOCKED` so concurrent holds on the same seat
serialize correctly instead of racing.

A seat counts as acquirable if it's `available`, OR it's `held` but its TTL
has already passed (lazy expiry) -- we don't wait for a background sweep to
notice. `release_expired_holds` is the eager counterpart (the "reconciler"
PRD 5.2 asks for): call it periodically so seats don't sit expired-but-held
until someone happens to try to hold them. Phase 2 (Subagent A) is expected
to run this on a schedule; Phase 1 exposes it as a plain function/endpoint so
tests can drive it directly and deterministically.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.state_machine import SeatStatus, assert_legal_transition
from app.models.seat_state import SeatState


class HoldError(Exception):
    """Raised when not every requested seat could be held. Holds are
    all-or-nothing: if any seat in the request is unavailable, none of them
    are held and the transaction is rolled back -- a buyer selecting 4 seats
    should never end up holding a random 3 of them."""

    def __init__(self, unavailable_seat_ids: list[uuid.UUID]):
        self.unavailable_seat_ids = unavailable_seat_ids
        super().__init__(f"seats unavailable: {unavailable_seat_ids}")


class ReleaseError(Exception):
    """Raised when a seat could not be released because it isn't held by
    the requesting session (already released, expired, sold, or held by
    someone else)."""

    def __init__(self, unreleasable_seat_ids: list[uuid.UUID]):
        self.unreleasable_seat_ids = unreleasable_seat_ids
        super().__init__(f"seats not releasable: {unreleasable_seat_ids}")


async def hold_seats(
    db: AsyncSession,
    event_id: uuid.UUID,
    session_id: uuid.UUID,
    seat_ids: list[uuid.UUID],
    ttl_seconds: int,
) -> tuple[list[uuid.UUID], datetime]:
    if not seat_ids:
        return [], datetime.now(UTC)

    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)

    stmt = (
        select(SeatState)
        .where(SeatState.event_id == event_id, SeatState.seat_id.in_(seat_ids))
        .with_for_update(skip_locked=True)
    )
    rows = {row.seat_id: row for row in (await db.execute(stmt)).scalars()}

    unavailable: list[uuid.UUID] = []
    for seat_id in seat_ids:
        row = rows.get(seat_id)
        if row is None:
            # Either no seat_state row exists for this seat/event, or another
            # in-flight transaction holds its row lock right now (SKIP LOCKED
            # excludes it silently). Either way it's not acquirable.
            unavailable.append(seat_id)
            continue
        expired = row.status == SeatStatus.HELD and row.held_until is not None and row.held_until < now
        if row.status != SeatStatus.AVAILABLE and not expired:
            unavailable.append(seat_id)

    if unavailable:
        await db.rollback()
        raise HoldError(unavailable)

    for seat_id in seat_ids:
        row = rows[seat_id]
        assert_legal_transition(SeatStatus.AVAILABLE, SeatStatus.HELD)
        row.status = SeatStatus.HELD
        row.held_by_session = session_id
        row.held_until = expires_at
        row.version += 1

    await db.commit()
    return list(seat_ids), expires_at


async def release_seats(
    db: AsyncSession,
    event_id: uuid.UUID,
    session_id: uuid.UUID,
    seat_ids: list[uuid.UUID],
) -> list[uuid.UUID]:
    if not seat_ids:
        return []

    stmt = (
        select(SeatState)
        .where(SeatState.event_id == event_id, SeatState.seat_id.in_(seat_ids))
        .with_for_update(skip_locked=True)
    )
    rows = {row.seat_id: row for row in (await db.execute(stmt)).scalars()}

    unreleasable: list[uuid.UUID] = []
    for seat_id in seat_ids:
        row = rows.get(seat_id)
        if row is None or row.status != SeatStatus.HELD or row.held_by_session != session_id:
            unreleasable.append(seat_id)

    if unreleasable:
        await db.rollback()
        raise ReleaseError(unreleasable)

    for seat_id in seat_ids:
        row = rows[seat_id]
        assert_legal_transition(SeatStatus.HELD, SeatStatus.AVAILABLE)
        row.status = SeatStatus.AVAILABLE
        row.held_by_session = None
        row.held_until = None
        row.version += 1

    await db.commit()
    return list(seat_ids)


async def release_expired_holds(db: AsyncSession, event_id: uuid.UUID | None = None) -> int:
    """The reconciler (PRD 5.2): sweep seats whose hold TTL has passed and
    return them to `available`. Safe to call repeatedly/concurrently --
    SKIP LOCKED means overlapping sweeps just split the work."""
    now = datetime.now(UTC)
    stmt = select(SeatState).where(SeatState.status == SeatStatus.HELD, SeatState.held_until < now)
    if event_id is not None:
        stmt = stmt.where(SeatState.event_id == event_id)
    stmt = stmt.with_for_update(skip_locked=True)

    rows = list((await db.execute(stmt)).scalars())
    for row in rows:
        assert_legal_transition(SeatStatus.HELD, SeatStatus.AVAILABLE)
        row.status = SeatStatus.AVAILABLE
        row.held_by_session = None
        row.held_until = None
        row.version += 1

    await db.commit()
    return len(rows)
