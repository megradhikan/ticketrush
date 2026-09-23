import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.state_machine import SeatStatus
from app.db.base import Base


class SeatState(Base):
    """
    Live seat availability for one seat within one event. This table is the
    durable source of truth for the state machine in app.core.state_machine
    (available -> held -> sold, held -> available). Postgres, not Redis, is
    authoritative here -- see PRD 4.2.

    The CHECK constraint below enforces the state machine's field-level
    invariants at the database layer, so a bug in application code cannot
    produce a row that's inconsistent with its own status (e.g. "held" with
    no session or no expiry). This is on top of -- not instead of --
    `assert_legal_transition` guarding every status *transition* in code.

    `version` is bumped on every transition and is used for optimistic
    locking / detecting stale reads (PRD section 9), independent of whichever
    row-locking strategy (Postgres SELECT FOR UPDATE vs Redis-first) acquired
    the write.
    """

    __tablename__ = "seat_state"
    __table_args__ = (
        CheckConstraint(
            "(status = 'available' AND held_by_session IS NULL AND held_until IS NULL) OR "
            "(status = 'held' AND held_by_session IS NOT NULL AND held_until IS NOT NULL) OR "
            "(status = 'sold' AND held_by_session IS NOT NULL AND held_until IS NULL)",
            name="ck_seat_state_status_fields_consistent",
        ),
        Index("ix_seat_state_event_status", "event_id", "status"),
        Index("ix_seat_state_held_until", "held_until"),
    )

    seat_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("seats.id"), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("events.id"), primary_key=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=SeatStatus.AVAILABLE)
    held_by_session: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    held_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()", onupdate=lambda: datetime.now(UTC)
    )

    seat: Mapped["Seat"] = relationship()
    event: Mapped["Event"] = relationship()
