"""
Idempotent checkout (PRD 5.3). Design choice, documented here since the PRD
leaves it open: on payment failure, held seats are released back to
`available` immediately (not held for retry) -- an interview-favorite
tradeoff to have an opinion on. Reasoning: a failed payment is very often a
declined card, not a transient blip, and the PRD's waiting room already
exists to be fair about scarce inventory, so silently reserving a seat for a
buyer whose payment just failed advantages them over everyone still in queue.
The idempotency_key still resolves to that failed Order on retry (matching
how real payment processors treat idempotency: same key -> same recorded
outcome), so a client retry doesn't get charged twice, it just needs a new
key to try again with different seats/payment info.

Idempotency itself is enforced two ways:
1. Fast path: look up the order by idempotency_key before doing any work.
2. Race backstop: `orders.idempotency_key` has a DB unique constraint, so if
   two concurrent requests both miss the fast-path check, the loser's INSERT
   raises IntegrityError and we re-fetch and return the winner's order. This
   is what actually guarantees invariant 4 under concurrency, not the
   fast-path check (which is just an optimization to avoid doing the seat
   work twice).
"""

import random
import uuid
from asyncio import sleep
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.state_machine import SeatStatus, assert_legal_transition
from app.models.order import Order, OrderSeat, OrderStatus
from app.models.seat_state import SeatState
from app.services.realtime import publish_seat_diffs


class CheckoutError(Exception):
    """Raised when the requested seats aren't all currently held by this
    session (already expired, released, sold, or never held). No order is
    created and no seat state changes."""

    def __init__(self, seat_ids: list[uuid.UUID]):
        self.seat_ids = seat_ids
        super().__init__(f"seats not held by session: {seat_ids}")


@dataclass
class PaymentResult:
    succeeded: bool


async def simulate_payment(
    failure_rate: float, latency_ms: int, rng: random.Random | None = None
) -> PaymentResult:
    """Pluggable so the Phase 3 simulator can inject a seeded `rng` for
    deterministic fault injection instead of this using global randomness."""
    r = rng or random.Random()
    await sleep(latency_ms / 1000)
    return PaymentResult(succeeded=r.random() >= failure_rate)


async def checkout(
    db: AsyncSession,
    event_id: uuid.UUID,
    session_id: uuid.UUID,
    seat_ids: list[uuid.UUID],
    idempotency_key: str,
    payment_failure_rate: float,
    payment_latency_ms: int,
    rng: random.Random | None = None,
) -> Order:
    existing = await db.scalar(select(Order).where(Order.idempotency_key == idempotency_key))
    if existing is not None:
        return existing

    # Blocking FOR UPDATE (not SKIP LOCKED): a concurrent duplicate request
    # with the *same* idempotency key must wait for the first one to finish
    # committing rather than skip the locked rows and see "not held". Once
    # unblocked it re-checks the idempotency key below before concluding the
    # seats genuinely aren't held.
    # ORDER BY seat_id gives every transaction the same lock-acquisition
    # order, so two checkouts with overlapping seat sets can't deadlock by
    # locking rows in opposite orders.
    stmt = (
        select(SeatState)
        .where(SeatState.event_id == event_id, SeatState.seat_id.in_(seat_ids))
        .order_by(SeatState.seat_id)
        .with_for_update()
    )
    rows = {row.seat_id: row for row in (await db.execute(stmt)).scalars()}

    not_held: list[uuid.UUID] = []
    for seat_id in seat_ids:
        row = rows.get(seat_id)
        if row is None or row.status != SeatStatus.HELD or row.held_by_session != session_id:
            not_held.append(seat_id)

    if not_held:
        # rollback() (unlike commit(), regardless of expire_on_commit) always
        # expires every object already loaded on this session, so query
        # AFTER rolling back -- otherwise the "existing" order we're about to
        # return gets expired out from under us and a later sync attribute
        # access on it blows up trying to lazy-refresh outside an await.
        await db.rollback()
        existing = await db.scalar(select(Order).where(Order.idempotency_key == idempotency_key))
        if existing is not None:
            return existing
        raise CheckoutError(not_held)

    payment = await simulate_payment(payment_failure_rate, payment_latency_ms, rng)

    # Assign the id explicitly rather than relying on the model's Python-side
    # default: that default is only materialized onto `order.id` once the
    # INSERT actually flushes, which is too late to reference from the
    # OrderSeat rows we're about to build in this same unit of work.
    order_id = uuid.uuid4()
    order = Order(
        id=order_id,
        session_id=session_id,
        idempotency_key=idempotency_key,
        status=OrderStatus.PAID if payment.succeeded else OrderStatus.FAILED,
    )
    db.add(order)

    for seat_id in seat_ids:
        row = rows[seat_id]
        if payment.succeeded:
            assert_legal_transition(SeatStatus.HELD, SeatStatus.SOLD)
            row.status = SeatStatus.SOLD
            row.held_until = None
            # held_by_session is left set to record the buyer on the sold seat.
        else:
            assert_legal_transition(SeatStatus.HELD, SeatStatus.AVAILABLE)
            row.status = SeatStatus.AVAILABLE
            row.held_by_session = None
            row.held_until = None
        row.version += 1
        db.add(OrderSeat(order_id=order_id, seat_id=seat_id))

    try:
        await db.commit()
    except IntegrityError:
        # Lost the idempotency-key race: someone else committed first.
        await db.rollback()
        winner = await db.scalar(select(Order).where(Order.idempotency_key == idempotency_key))
        if winner is None:
            raise
        return winner

    await db.refresh(order)
    await publish_seat_diffs(rows.values())  # realtime fanout (PRD 4.2), best-effort: sold or released
    return order
