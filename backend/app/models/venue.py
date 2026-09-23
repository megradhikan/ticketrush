import uuid

from sqlalchemy import JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Venue(Base):
    __tablename__ = "venues"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Full seat map layout for frontend rendering: sections -> rows -> seat
    # metadata, section boundaries for canvas drawing, etc.
    layout_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    seats: Mapped[list["Seat"]] = relationship(back_populates="venue")
    events: Mapped[list["Event"]] = relationship(back_populates="venue")
