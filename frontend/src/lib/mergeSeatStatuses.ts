import type { SeatOut } from "../types";
import type { SeatStreamState } from "./seatStreamReducer";

/**
 * Overlays live stream state (source of truth once connected) onto the
 * initial REST-fetched seat list (static metadata: section/row/x/y/price).
 * Only status/held_until are ever replaced.
 */
export function mergeSeatStatuses(seats: SeatOut[], live: SeatStreamState): SeatOut[] {
  if (live.size === 0) return seats;
  return seats.map((seat) => {
    const entry = live.get(seat.id);
    if (!entry) return seat;
    if (entry.status === seat.status && entry.held_until === seat.held_until) {
      return seat;
    }
    return { ...seat, status: entry.status, held_until: entry.held_until };
  });
}
