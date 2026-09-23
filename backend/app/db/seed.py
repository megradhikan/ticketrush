"""Venue/event/seat factory shared by tests, the simulator, and local dev
seeding. Not part of the correctness core, but every phase needs some way to
materialize a venue -- keeping one implementation here avoids each phase (or
subagent) inventing a slightly different one."""

import string
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.state_machine import SeatStatus
from app.models.event import Event, EventStatus
from app.models.seat import Seat
from app.models.seat_state import SeatState
from app.models.venue import Venue


async def create_venue_with_seats(
    db: AsyncSession,
    name: str = "Test Arena",
    sections: int = 2,
    rows_per_section: int = 5,
    seats_per_row: int = 10,
    price_tier: str = "GA",
) -> tuple[Venue, Event, list[Seat]]:
    venue = Venue(id=uuid.uuid4(), name=name, layout_json={"sections": sections})
    db.add(venue)

    event = Event(
        id=uuid.uuid4(),
        venue_id=venue.id,
        name=f"{name} Onsale",
        onsale_start_at=datetime.now(UTC),
        status=EventStatus.ONSALE,
    )
    db.add(event)

    seats: list[Seat] = []
    row_letters = string.ascii_uppercase
    for section_idx in range(sections):
        section_name = f"Section {section_idx + 1}"
        for row_idx in range(rows_per_section):
            row_name = row_letters[row_idx % len(row_letters)]
            for seat_num in range(1, seats_per_row + 1):
                seat = Seat(
                    id=uuid.uuid4(),
                    venue_id=venue.id,
                    section=section_name,
                    row=row_name,
                    seat_number=seat_num,
                    price_tier=price_tier,
                    x=float(section_idx * (seats_per_row + 2) + seat_num),
                    y=float(row_idx),
                    attributes={"aisle": seat_num in (1, seats_per_row)},
                )
                seats.append(seat)
    db.add_all(seats)

    for seat in seats:
        db.add(SeatState(seat_id=seat.id, event_id=event.id, status=SeatStatus.AVAILABLE, version=0))

    await db.commit()
    return venue, event, seats
