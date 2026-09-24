import type { SeatStatus } from "../types";

/**
 * This is the FROZEN wire contract for GET /events/{event_id}/stream, as
 * specified by the teammate building the real WS server on a separate
 * branch. Do not change these shapes without re-syncing with them --
 * everything downstream (the reducer, the transport, the hook) is built
 * against exactly this.
 *
 *   On connect:
 *     {"type": "snapshot", "seats": [{"seat_id", "status", "held_until"}, ...]}
 *   On any change:
 *     {"type": "diff", "seat_id", "status", "held_until"}
 *     {"type": "diff_batch", "diffs": [{"seat_id", "status", "held_until"}, ...]}
 */

export interface SeatStreamSeatEntry {
  seat_id: string;
  status: SeatStatus;
  held_until: string | null;
}

export interface SnapshotMessage {
  type: "snapshot";
  seats: SeatStreamSeatEntry[];
}

export interface DiffMessage {
  type: "diff";
  seat_id: string;
  status: SeatStatus;
  held_until: string | null;
}

export interface DiffBatchMessage {
  type: "diff_batch";
  diffs: Array<{ seat_id: string; status: SeatStatus; held_until: string | null }>;
}

export type SeatStreamMessage = SnapshotMessage | DiffMessage | DiffBatchMessage;

export interface SeatStreamEntry {
  status: SeatStatus;
  held_until: string | null;
}

/** seat_id -> latest known {status, held_until} per the live stream. */
export type SeatStreamState = ReadonlyMap<string, SeatStreamEntry>;

export function createEmptySeatStreamState(): SeatStreamState {
  return new Map();
}

/**
 * Reducer over the frozen message contract: a snapshot fully replaces state,
 * a diff/diff_batch merges into it. Pure and immutable so it's trivial to
 * unit test (given a snapshot + a sequence of diffs, does seat state end up
 * correct?) independent of any transport (real WS or mock).
 */
export function applySeatStreamMessage(
  state: SeatStreamState,
  message: SeatStreamMessage
): SeatStreamState {
  switch (message.type) {
    case "snapshot": {
      const next = new Map<string, SeatStreamEntry>();
      for (const seat of message.seats) {
        next.set(seat.seat_id, { status: seat.status, held_until: seat.held_until });
      }
      return next;
    }
    case "diff": {
      const next = new Map(state);
      next.set(message.seat_id, {
        status: message.status,
        held_until: message.held_until,
      });
      return next;
    }
    case "diff_batch": {
      const next = new Map(state);
      for (const diff of message.diffs) {
        next.set(diff.seat_id, { status: diff.status, held_until: diff.held_until });
      }
      return next;
    }
    default:
      return state;
  }
}
