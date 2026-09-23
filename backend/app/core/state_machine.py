"""
The seat state machine. This is the single source of truth for legal seat
transitions (PRD section 4.1). Every code path that changes seat_state.status
-- Postgres-only holds, Redis-first holds, checkout, the hold reconciler, the
allocator -- must go through `assert_legal_transition` (or replicate these
exact rules) rather than inventing its own notion of what's legal.

States: available -> held -> sold
        held -> available   (TTL expiry or explicit release)

No other transitions are legal. In particular:
  - sold is terminal. A sold seat can never move back to held or available.
  - available -> sold directly is not legal; a seat must be held first.
  - held -> held (re-holding by the same or a different session) is not a
    transition this module allows; callers must release first.
"""

from enum import StrEnum


class SeatStatus(StrEnum):
    AVAILABLE = "available"
    HELD = "held"
    SOLD = "sold"


_LEGAL_TRANSITIONS: dict[SeatStatus, frozenset[SeatStatus]] = {
    SeatStatus.AVAILABLE: frozenset({SeatStatus.HELD}),
    SeatStatus.HELD: frozenset({SeatStatus.SOLD, SeatStatus.AVAILABLE}),
    SeatStatus.SOLD: frozenset(),
}


class IllegalTransitionError(Exception):
    def __init__(self, from_status: SeatStatus, to_status: SeatStatus):
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"illegal seat transition: {from_status} -> {to_status}")


def assert_legal_transition(from_status: SeatStatus, to_status: SeatStatus) -> None:
    if to_status not in _LEGAL_TRANSITIONS.get(from_status, frozenset()):
        raise IllegalTransitionError(from_status, to_status)


def is_legal_transition(from_status: SeatStatus, to_status: SeatStatus) -> bool:
    return to_status in _LEGAL_TRANSITIONS.get(from_status, frozenset())
