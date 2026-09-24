import uuid

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    CheckoutRequest,
    CheckoutResponse,
    HoldRequest,
    HoldResponse,
    OrderOut,
    QueueJoinRequest,
    QueueJoinResponse,
    QueueStatusResponse,
    ReleaseRequest,
    ReleaseResponse,
    SeatOut,
    SessionOut,
)
from app.core.config import get_settings
from app.db.redis_client import get_redis
from app.db.session import AsyncSessionLocal, get_db
from app.models.buyer_session import BuyerSession
from app.models.order import OrderSeat
from app.models.seat import Seat
from app.models.seat_state import SeatState
from app.services import checkout as checkout_service
from app.services import holds as holds_service
from app.services import queue as queue_service
from app.services import redis_holds as redis_holds_service
from app.services.ws_manager import connection_manager

router = APIRouter()
settings = get_settings()

# PRD 5.2: two interchangeable hold strategies, selected by settings.hold_strategy.
# NOTE for reviewers: the waiting room (queue_service) is intentionally NOT
# wired as a gate in front of hold/checkout below -- see the docstring on
# queue_service.require_admission for why, and how it would be added.


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
async def hold_seats(
    event_id: uuid.UUID,
    req: HoldRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> HoldResponse:
    try:
        if settings.hold_strategy == "redis":
            held, expires_at = await redis_holds_service.hold_seats(
                db, redis, event_id, req.session_id, req.seat_ids, settings.hold_ttl_seconds
            )
        else:
            held, expires_at = await holds_service.hold_seats(
                db, event_id, req.session_id, req.seat_ids, settings.hold_ttl_seconds
            )
    except holds_service.HoldError as e:
        raise HTTPException(status_code=409, detail={"unavailable_seat_ids": [str(s) for s in e.unavailable_seat_ids]})
    return HoldResponse(held_seats=held, expires_at=expires_at)


@router.post("/events/{event_id}/seats/release", response_model=ReleaseResponse)
async def release_seats(
    event_id: uuid.UUID,
    req: ReleaseRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> ReleaseResponse:
    try:
        if settings.hold_strategy == "redis":
            released = await redis_holds_service.release_seats(db, redis, event_id, req.session_id, req.seat_ids)
        else:
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


@router.post("/events/{event_id}/queue/join", response_model=QueueJoinResponse)
async def queue_join(
    event_id: uuid.UUID,
    req: QueueJoinRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> QueueJoinResponse:
    try:
        result = await queue_service.join_queue(
            db,
            redis,
            event_id,
            req.session_id,
            settings.waiting_room_batch_size,
            settings.waiting_room_batch_interval_seconds,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return QueueJoinResponse(position=result.position, est_admit_at=result.est_admit_at)


@router.get("/events/{event_id}/queue/status", response_model=QueueStatusResponse)
async def queue_status(
    event_id: uuid.UUID,
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> QueueStatusResponse:
    try:
        result = await queue_service.get_status(db, redis, event_id, session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return QueueStatusResponse(position=result.position, admitted=result.admitted, admission_token=result.admission_token)


@router.websocket("/events/{event_id}/stream")
async def event_stream(websocket: WebSocket, event_id: uuid.UUID) -> None:
    """PRD 4.2/10: WS seat-map diffs. On connect, sends a full snapshot
    queried live from Postgres (source of truth); after that, forwards
    `diff`/`diff_batch` messages published by holds/checkout via Redis
    pub/sub fanout (see app.services.realtime, app.services.ws_manager).

    Uses a short-lived session for the snapshot query only -- not
    `Depends(get_db)`, which would hold a pooled connection checked out for
    the whole (potentially long) lifetime of the socket.
    """
    await websocket.accept()

    async with AsyncSessionLocal() as db:
        stmt = select(SeatState).where(SeatState.event_id == event_id)
        rows = list((await db.execute(stmt)).scalars())

    await websocket.send_json(
        {
            "type": "snapshot",
            "seats": [
                {
                    "seat_id": str(row.seat_id),
                    "status": row.status,
                    "held_until": row.held_until.isoformat() if row.held_until else None,
                }
                for row in rows
            ],
        }
    )

    await connection_manager.connect(event_id, websocket)
    try:
        while True:
            # Clients don't send anything on this stream today; this just
            # blocks until the client disconnects (or sends something we
            # ignore), so we notice and clean up.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await connection_manager.disconnect(event_id, websocket)
