import uuid

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Seat(Base):
    """
    A physical seat in a venue. Seat identity (section/row/number) and
    layout metadata are static and event-independent; live availability
    lives in SeatState, keyed by (seat_id, event_id).
    """

    __tablename__ = "seats"
    __table_args__ = (
        # Fast per-row adjacency scans for the allocator (section 7): all
        # seats in a given section+row, ordered by seat_number.
        {"comment": "static seat inventory; one row per physical seat"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("venues.id"), nullable=False)
    section: Mapped[str] = mapped_column(String(64), nullable=False)
    row: Mapped[str] = mapped_column(String(16), nullable=False)
    seat_number: Mapped[int] = mapped_column(Integer, nullable=False)
    price_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    x: Mapped[float] = mapped_column(nullable=False)
    y: Mapped[float] = mapped_column(nullable=False)
    # e.g. {"aisle": true, "view": "obstructed", "accessible": false}
    attributes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    venue: Mapped["Venue"] = relationship(back_populates="seats")
