import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    CheckoutRequest,
    CheckoutResponse,
    HoldRequest,
    HoldResponse,
    OrderOut,
    ReleaseRequest,
    ReleaseResponse,
    SeatOut,
    SessionOut,
)
from app.core.config import get_settings
from app.db.session import get_db
from app.models.buyer_session import BuyerSession
from app.models.order import OrderSeat
from app.models.seat import Seat
from app.models.seat_state import SeatState
from app.services import checkout as checkout_service
from app.services import holds as holds_service

router = APIRouter()
settings = get_settings()


@router.post("/sessions", response_model=SessionOut)
async def create_session(db: AsyncSession = Depends(get_db)) -> BuyerSession:
    """Mock auth (PRD non-goals: full auth is out of scope). Creates a bare
    buyer session with no waiting-room admission gate -- Phase 2 adds that."""
    session = BuyerSession(id=uuid.uuid4())
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


@router.get("/events/{event_id}/seats", response_model=list[SeatOut])
async def get_event_seats(event_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list[SeatOut]:
    stmt = select(Seat, SeatState).join(SeatState, SeatState.seat_id == Seat.id).where(
        SeatState.event_id == event_id
    )
    rows = (await db.execute(stmt)).all()
    if not rows:
        raise HTTPException(status_code=404, detail="event not found or has no seats")
    return [
        SeatOut(
            id=seat.id,
            section=seat.section,
            row=seat.row,
            seat_number=seat.seat_number,
            price_tier=seat.price_tier,
            x=seat.x,
            y=seat.y,
            attributes=seat.attributes,
            status=state.status,
            held_until=state.held_until,
        )
        for seat, state in rows
    ]


@router.post("/events/{event_id}/seats/hold", response_model=HoldResponse)
async def hold_seats(event_id: uuid.UUID, req: HoldRequest, db: AsyncSession = Depends(get_db)) -> HoldResponse:
    try:
        held, expires_at = await holds_service.hold_seats(
            db, event_id, req.session_id, req.seat_ids, settings.hold_ttl_seconds
        )
    except holds_service.HoldError as e:
        raise HTTPException(status_code=409, detail={"unavailable_seat_ids": [str(s) for s in e.unavailable_seat_ids]})
    return HoldResponse(held_seats=held, expires_at=expires_at)


@router.post("/events/{event_id}/seats/release", response_model=ReleaseResponse)
async def release_seats(
    event_id: uuid.UUID, req: ReleaseRequest, db: AsyncSession = Depends(get_db)
) -> ReleaseResponse:
    try:
        released = await holds_service.release_seats(db, event_id, req.session_id, req.seat_ids)
    except holds_service.ReleaseError as e:
        raise HTTPException(
            status_code=409, detail={"unreleasable_seat_ids": [str(s) for s in e.unreleasable_seat_ids]}
        )
    return ReleaseResponse(released_seats=released)


@router.post("/events/{event_id}/checkout", response_model=CheckoutResponse)
async def do_checkout(
    event_id: uuid.UUID, req: CheckoutRequest, db: AsyncSession = Depends(get_db)
) -> CheckoutResponse:
    try:
        order = await checkout_service.checkout(
            db,
            event_id,
            req.session_id,
            req.seat_ids,
            req.idempotency_key,
            settings.checkout_payment_failure_rate,
            settings.checkout_payment_latency_ms,
        )
    except checkout_service.CheckoutError as e:
        raise HTTPException(status_code=409, detail={"seats_not_held": [str(s) for s in e.seat_ids]})

    seat_ids_stmt = select(OrderSeat.seat_id).where(OrderSeat.order_id == order.id)
    seat_ids = list((await db.execute(seat_ids_stmt)).scalars())

    return CheckoutResponse(
        order=OrderOut(id=order.id, status=order.status, seat_ids=seat_ids, created_at=order.created_at)
    )
