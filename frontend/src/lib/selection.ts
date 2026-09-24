import type { SeatOut } from "../types";

/**
 * Toggles a single seat in/out of the client-side selection set. No-op for
 * seats that are not currently available (held/sold seats can't be selected).
 * Pure and immutable: always returns a new Set (or the same reference when
 * nothing changed), so it's easy to test and safe to use in a React reducer.
 */
export function toggleSeatSelection(
  selected: ReadonlySet<string>,
  seat: SeatOut
): Set<string> {
  if (seat.status !== "available") {
    return new Set(selected);
  }
  const next = new Set(selected);
  if (next.has(seat.id)) {
    next.delete(seat.id);
  } else {
    next.add(seat.id);
  }
  return next;
}

/**
 * Removes seat ids that a 409 hold response reported as unavailable from the
 * current selection (someone else grabbed them between select and hold).
 * Returns both the updated set and the list actually removed, so callers can
 * surface a precise message to the user instead of failing silently.
 */
export function removeUnavailableFromSelection(
  selected: ReadonlySet<string>,
  unavailableSeatIds: readonly string[]
): { next: Set<string>; removed: string[] } {
  const next = new Set(selected);
  const removed: string[] = [];
  for (const id of unavailableSeatIds) {
    if (next.delete(id)) {
      removed.push(id);
    }
  }
  return { next, removed };
}
