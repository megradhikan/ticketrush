"""Import every model module here so SQLAlchemy's mapper registry sees all
classes before relationships (which use string forward-refs) are configured.
Alembic's env.py imports this module to discover metadata for autogenerate."""

from app.models.buyer_session import BuyerSession
from app.models.event import Event, EventStatus
from app.models.order import Order, OrderSeat, OrderStatus
from app.models.seat import Seat
from app.models.seat_state import SeatState
from app.models.sim_run import SimRun
from app.models.venue import Venue

__all__ = [
    "BuyerSession",
    "Event",
    "EventStatus",
    "Order",
    "OrderSeat",
    "OrderStatus",
    "Seat",
    "SeatState",
    "SimRun",
    "Venue",
]
