"""
The correctness core (PRD 5.4). Every test here runs real concurrent
coroutines, each with its OWN Postgres connection/transaction (via
new_db_session), against the actually-running database -- this is what makes
these tests worth anything: they exercise real SELECT FOR UPDATE SKIP LOCKED
contention, not cooperative asyncio scheduling within a single transaction.

1. No seat is ever sold more than once.
2. No seat is held by two different sessions simultaneously.
3. Every held seat either transitions to sold or eventually returns to
   available (no permanent leaks).
4. Checkout is idempotent: N identical requests produce exactly 1 order.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.state_machine import SeatStatus
from app.models.order import Order, OrderSeat
from app.models.seat_state import SeatState
from app.services import checkout as checkout_service
from app.services import holds as holds_service


async def test_invariant_1_no_double_sell(new_db_session, venue_with_seats, make_session, db):
    _, event, seats = venue_with_seats
    seat = seats[0]
    n_buyers = 25
    session_ids = await asyncio.gather(*(make_session() for _ in range(n_buyers)))

    async def attempt(session_id: uuid.UUID) -> str:
        s = new_db_session()
        try:
            await holds_service.hold_seats(s, event.id, session_id, [seat.id], ttl_seconds=300)
        except holds_service.HoldError:
            return "hold_failed"
        try:
            order = await checkout_service.checkout(
                s, event.id, session_id, [seat.id], str(uuid.uuid4()), 0.0, 0
            )
        except checkout_service.CheckoutError:
            return "checkout_failed"
        return order.status

    results = await asyncio.gather(*(attempt(sid) for sid in session_ids))

    assert results.count("paid") == 1, f"expected exactly 1 successful sale, got: {results}"

    sold_count = await db.scalar(
        select(func.count()).select_from(SeatState).where(SeatState.seat_id == seat.id, SeatState.status == "sold")
    )
    assert sold_count == 1

    order_count = await db.scalar(
        select(func.count()).select_from(OrderSeat).where(OrderSeat.seat_id == seat.id)
    )
    assert order_count == 1, "seat must appear on exactly one order"


async def test_invariant_2_no_double_hold(new_db_session, venue_with_seats, make_session, db):
    _, event, seats = venue_with_seats
    seat = seats[0]
    n_buyers = 25
    session_ids = await asyncio.gather(*(make_session() for _ in range(n_buyers)))

    async def attempt(session_id: uuid.UUID) -> bool:
        s = new_db_session()
        try:
            await holds_service.hold_seats(s, event.id, session_id, [seat.id], ttl_seconds=300)
            return True
        except holds_service.HoldError:
            return False

    results = await asyncio.gather(*(attempt(sid) for sid in session_ids))
    assert sum(results) == 1, f"expected exactly 1 successful hold, got {sum(results)} of {n_buyers}"

    state = await db.get(SeatState, {"seat_id": seat.id, "event_id": event.id})
    assert state.status == SeatStatus.HELD
    assert state.held_by_session is not None


async def test_invariant_3_no_permanent_leak(venue_with_seats, make_session, db):
    _, event, seats = venue_with_seats
    seat = seats[0]
    session_id = await make_session()

    # Hold with a TTL that's already in the past by the time we sweep, to
    # deterministically simulate expiry without a real sleep.
    held, expires_at = await holds_service.hold_seats(db, event.id, session_id, [seat.id], ttl_seconds=300)
    assert held == [seat.id]

    state = await db.get(SeatState, {"seat_id": seat.id, "event_id": event.id})
    state.held_until = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()

    swept = await holds_service.release_expired_holds(db, event.id)
    assert swept == 1

    await db.refresh(state)
    assert state.status == SeatStatus.AVAILABLE
    assert state.held_by_session is None
    assert state.held_until is None


async def test_invariant_3_sold_seats_are_not_swept(venue_with_seats, make_session, db):
    """A seat that made it to `sold` must never be touched by the expiry
    reconciler, even though held_until was set before the sale."""
    _, event, seats = venue_with_seats
    seat = seats[0]
    session_id = await make_session()

    await holds_service.hold_seats(db, event.id, session_id, [seat.id], ttl_seconds=300)
    await checkout_service.checkout(db, event.id, session_id, [seat.id], str(uuid.uuid4()), 0.0, 0)

    swept = await holds_service.release_expired_holds(db, event.id)
    assert swept == 0

    state = await db.get(SeatState, {"seat_id": seat.id, "event_id": event.id})
    assert state.status == SeatStatus.SOLD


async def test_invariant_4_idempotent_checkout(new_db_session, venue_with_seats, make_session, db):
    _, event, seats = venue_with_seats
    seat = seats[0]
    session_id = await make_session()

    setup_db = new_db_session()
    await holds_service.hold_seats(setup_db, event.id, session_id, [seat.id], ttl_seconds=300)

    idem_key = str(uuid.uuid4())
    n_requests = 15

    async def attempt():
        s = new_db_session()
        return await checkout_service.checkout(s, event.id, session_id, [seat.id], idem_key, 0.0, 0)

    results = await asyncio.gather(*(attempt() for _ in range(n_requests)), return_exceptions=True)

    exceptions = [r for r in results if isinstance(r, Exception)]
    assert not exceptions, f"idempotent checkout should never raise, got: {exceptions}"

    order_ids = {r.id for r in results}
    assert len(order_ids) == 1, f"all {n_requests} identical requests must return the same order, got {order_ids}"

    count = await db.scalar(select(func.count()).select_from(Order).where(Order.idempotency_key == idem_key))
    assert count == 1, "exactly one order row must exist for this idempotency key"


async def test_invariant_4_checkout_failure_releases_seat_and_is_idempotent(
    new_db_session, venue_with_seats, make_session, db
):
    """Documents the payment-failure design choice (see services/checkout.py):
    a failed payment releases the seat immediately, and retrying with the
    same idempotency key returns the same failed order rather than
    re-attempting payment."""
    _, event, seats = venue_with_seats
    seat = seats[0]
    session_id = await make_session()

    await holds_service.hold_seats(db, event.id, session_id, [seat.id], ttl_seconds=300)

    idem_key = str(uuid.uuid4())
    order1 = await checkout_service.checkout(db, event.id, session_id, [seat.id], idem_key, 1.0, 0)
    assert order1.status == "failed"

    state = await db.get(SeatState, {"seat_id": seat.id, "event_id": event.id})
    assert state.status == SeatStatus.AVAILABLE

    order2 = await checkout_service.checkout(db, event.id, session_id, [seat.id], idem_key, 1.0, 0)
    assert order2.id == order1.id
    assert order2.status == "failed"

    count = await db.scalar(select(func.count()).select_from(Order).where(Order.idempotency_key == idem_key))
    assert count == 1
