import uuid
from datetime import datetime

from pydantic import BaseModel


class SeatOut(BaseModel):
    id: uuid.UUID
    section: str
    row: str
    seat_number: int
    price_tier: str
    x: float
    y: float
    attributes: dict
    status: str
    held_until: datetime | None = None

    model_config = {"from_attributes": True}


class SessionOut(BaseModel):
    id: uuid.UUID

    model_config = {"from_attributes": True}


class HoldRequest(BaseModel):
    session_id: uuid.UUID
    seat_ids: list[uuid.UUID]


class HoldResponse(BaseModel):
    held_seats: list[uuid.UUID]
    expires_at: datetime


class ReleaseRequest(BaseModel):
    session_id: uuid.UUID
    seat_ids: list[uuid.UUID]


class ReleaseResponse(BaseModel):
    released_seats: list[uuid.UUID]


class CheckoutRequest(BaseModel):
    session_id: uuid.UUID
    seat_ids: list[uuid.UUID]
    idempotency_key: str


class OrderOut(BaseModel):
    id: uuid.UUID
    status: str
    seat_ids: list[uuid.UUID]
    created_at: datetime


class CheckoutResponse(BaseModel):
    order: OrderOut


class QueueJoinRequest(BaseModel):
    session_id: uuid.UUID


class QueueJoinResponse(BaseModel):
    position: int
    est_admit_at: datetime


class QueueStatusResponse(BaseModel):
    position: int
    admitted: bool
    admission_token: str | None = None
