"""The same PRD 5.4 invariants as test_invariants.py, proven for the
Redis-first path (services/redis_holds.py) instead of the Postgres-only
path (services/holds.py). Each concurrent "buyer" gets its own Postgres
connection/transaction via new_db_session, same rationale as
test_invariants.py -- this has to exercise real contention, not cooperative
asyncio scheduling within one transaction. All attempts share one Redis
client (redis_client fixture), which mirrors how a real process would use a
single pooled client across concurrent requests.

Also covers the reconciler (redis_holds.reconcile), which has no Postgres-
only equivalent to piggyback tests on: the drift-repair cases PRD 5.2
explicitly calls out (Redis TTL fired but Postgres still shows held;
Postgres shows available but a live Redis hold key exists because
confirmation crashed).
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.state_machine import SeatStatus
from app.models.order import Order, OrderSeat
from app.models.seat_state import SeatState
from app.services import checkout as checkout_service
from app.services import redis_holds


async def test_invariant_1_no_double_sell_redis(new_db_session, venue_with_seats, make_session, db, redis_client):
    _, event, seats = venue_with_seats
    seat = seats[0]
    n_buyers = 25
    session_ids = await asyncio.gather(*(make_session() for _ in range(n_buyers)))

    async def attempt(session_id: uuid.UUID) -> str:
        s = new_db_session()
        try:
            await redis_holds.hold_seats(s, redis_client, event.id, session_id, [seat.id], ttl_seconds=300)
        except redis_holds.HoldError:
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

    order_count = await db.scalar(select(func.count()).select_from(OrderSeat).where(OrderSeat.seat_id == seat.id))
    assert order_count == 1, "seat must appear on exactly one order"


async def test_invariant_2_no_double_hold_redis(new_db_session, venue_with_seats, make_session, db, redis_client):
    _, event, seats = venue_with_seats
    seat = seats[0]
    n_buyers = 25
    session_ids = await asyncio.gather(*(make_session() for _ in range(n_buyers)))

    async def attempt(session_id: uuid.UUID) -> bool:
        s = new_db_session()
        try:
            await redis_holds.hold_seats(s, redis_client, event.id, session_id, [seat.id], ttl_seconds=300)
            return True
        except redis_holds.HoldError:
            return False

    results = await asyncio.gather(*(attempt(sid) for sid in session_ids))
    assert sum(results) == 1, f"expected exactly 1 successful hold, got {sum(results)} of {n_buyers}"

    state = await db.get(SeatState, {"seat_id": seat.id, "event_id": event.id})
    assert state.status == SeatStatus.HELD
    assert state.held_by_session is not None

    # Redis and Postgres must agree on who holds it.
    redis_val = await redis_client.get(redis_holds._hold_key(event.id, seat.id))
    assert redis_val == str(state.held_by_session)


async def test_hold_all_or_nothing_across_multiple_seats_redis(
    new_db_session, venue_with_seats, make_session, db, redis_client
):
    """A buyer holding N seats gets all N or none -- never a partial hold --
    at both the Redis layer and the Postgres confirmation layer."""
    _, event, seats = venue_with_seats
    contested_seat = seats[0]
    free_seats = seats[1:4]
    session_a = await make_session()
    session_b = await make_session()

    # session_a takes the contested seat first.
    await redis_holds.hold_seats(db, redis_client, event.id, session_a, [contested_seat.id], ttl_seconds=300)

    # session_b tries to hold [contested_seat, free1, free2, free3] -- must fail entirely.
    seat_ids = [contested_seat.id, *[s.id for s in free_seats]]
    s = new_db_session()
    try:
        await redis_holds.hold_seats(s, redis_client, event.id, session_b, seat_ids, ttl_seconds=300)
        assert False, "expected HoldError"
    except redis_holds.HoldError:
        pass

    # None of the free seats should be held by session_b in Postgres or Redis.
    for seat in free_seats:
        state = await db.get(SeatState, {"seat_id": seat.id, "event_id": event.id})
        assert state.status == SeatStatus.AVAILABLE, f"seat {seat.id} leaked a partial hold"
        redis_val = await redis_client.get(redis_holds._hold_key(event.id, seat.id))
        assert redis_val is None, f"seat {seat.id} has a leaked Redis hold key"


async def test_invariant_3_no_permanent_leak_redis(venue_with_seats, make_session, db, redis_client):
    _, event, seats = venue_with_seats
    seat = seats[0]
    session_id = await make_session()

    held, expires_at = await redis_holds.hold_seats(db, redis_client, event.id, session_id, [seat.id], ttl_seconds=300)
    assert held == [seat.id]

    # Simulate the hold having actually expired: backdate held_until in
    # Postgres (deterministic, no real sleep) and force the Redis key to
    # expire immediately, mirroring what a real TTL firing would leave
    # behind (key gone, index entry stale).
    state = await db.get(SeatState, {"seat_id": seat.id, "event_id": event.id})
    state.held_until = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    await redis_client.delete(redis_holds._hold_key(event.id, seat.id))

    report = await redis_holds.reconcile(db, redis_client, event.id)
    assert report.expired_released == 1
    # Whether the index entry is cleaned up as part of the expired-holds
    # sweep itself (release_expired_holds best-effort deletes the Redis key
    # and index entry for anything it releases) or by the separate
    # stale-index scan is an implementation detail -- either way it must be
    # gone by the time reconcile() returns.
    assert report.expired_released + report.stale_index_cleaned >= 1

    await db.refresh(state)
    assert state.status == SeatStatus.AVAILABLE
    assert state.held_by_session is None
    assert state.held_until is None

    # Index should be clean now -- nothing left to reconcile.
    is_member = await redis_client.sismember(redis_holds._index_key(event.id), str(seat.id))
    assert not is_member


async def test_reconciler_releases_orphaned_redis_hold_never_confirmed(
    venue_with_seats, make_session, db, redis_client
):
    """Simulates the crash window the module docstring describes: the Lua
    script won in Redis but the process died before the Postgres
    confirmation write landed. Postgres still shows `available`. The
    reconciler must release the orphaned Redis key rather than let it block
    real buyers forever (that would itself be a leaked hold, invariant 3)."""
    _, event, seats = venue_with_seats
    seat = seats[0]
    session_id = await make_session()

    keys = [redis_holds._hold_key(event.id, seat.id)]
    argv = [str(session_id), "300", redis_holds._index_key(event.id), str(seat.id)]
    won = await redis_client.eval(redis_holds._HOLD_SCRIPT, 1, *keys, *argv)
    assert won == 1

    state = await db.get(SeatState, {"seat_id": seat.id, "event_id": event.id})
    assert state.status == SeatStatus.AVAILABLE, "Postgres was never confirmed -- this is the drift case"

    report = await redis_holds.reconcile(db, redis_client, event.id)
    assert report.orphaned_redis_released == 1

    exists = await redis_client.exists(keys[0])
    assert exists == 0

    # A real buyer must now be able to hold the seat -- it's not leaked.
    held, _ = await redis_holds.hold_seats(db, redis_client, event.id, session_id, [seat.id], ttl_seconds=300)
    assert held == [seat.id]


async def test_invariant_3_sold_seats_are_not_swept_redis(venue_with_seats, make_session, db, redis_client):
    """checkout_service is Postgres-only and doesn't know about Redis, so
    the Redis hold key set at hold time is left dangling (with its original
    TTL) after a seat sells -- reconcile's "Postgres disagrees" case (b)
    correctly treats that as drift and cleans it up (harmless: the seat
    stays SOLD in Postgres throughout, which is the only thing that
    matters -- see the module docstring on why Postgres confirmation, not
    the Redis key, is the actual correctness gate for a resale)."""
    _, event, seats = venue_with_seats
    seat = seats[0]
    session_id = await make_session()

    await redis_holds.hold_seats(db, redis_client, event.id, session_id, [seat.id], ttl_seconds=300)
    await checkout_service.checkout(db, event.id, session_id, [seat.id], str(uuid.uuid4()), 0.0, 0)

    report = await redis_holds.reconcile(db, redis_client, event.id)
    assert report.expired_released == 0

    state = await db.get(SeatState, {"seat_id": seat.id, "event_id": event.id})
    assert state.status == SeatStatus.SOLD, "reconcile must never touch a sold seat's Postgres status"

    # A subsequent hold attempt on the now-sold seat must still fail, Redis
    # key cleaned up or not -- Postgres confirmation is the real gate.
    other_session = await make_session()
    try:
        await redis_holds.hold_seats(db, redis_client, event.id, other_session, [seat.id], ttl_seconds=300)
        assert False, "must not be able to hold a sold seat"
    except redis_holds.HoldError:
        pass


async def test_invariant_4_idempotent_checkout_redis(new_db_session, venue_with_seats, make_session, db, redis_client):
    _, event, seats = venue_with_seats
    seat = seats[0]
    session_id = await make_session()

    setup_db = new_db_session()
    await redis_holds.hold_seats(setup_db, redis_client, event.id, session_id, [seat.id], ttl_seconds=300)

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


async def test_release_seats_redis_clears_both_layers(venue_with_seats, make_session, db, redis_client):
    _, event, seats = venue_with_seats
    seat = seats[0]
    session_id = await make_session()

    await redis_holds.hold_seats(db, redis_client, event.id, session_id, [seat.id], ttl_seconds=300)
    released = await redis_holds.release_seats(db, redis_client, event.id, session_id, [seat.id])
    assert released == [seat.id]

    state = await db.get(SeatState, {"seat_id": seat.id, "event_id": event.id})
    assert state.status == SeatStatus.AVAILABLE

    exists = await redis_client.exists(redis_holds._hold_key(event.id, seat.id))
    assert exists == 0
    is_member = await redis_client.sismember(redis_holds._index_key(event.id), str(seat.id))
    assert not is_member
