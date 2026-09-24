import { describe, expect, it } from "vitest";

import {
  applySeatStreamMessage,
  createEmptySeatStreamState,
  type SeatStreamMessage,
} from "./seatStreamReducer";

describe("applySeatStreamMessage", () => {
  it("a snapshot fully replaces state with the seats it lists", () => {
    const snapshot: SeatStreamMessage = {
      type: "snapshot",
      seats: [
        { seat_id: "a", status: "available", held_until: null },
        { seat_id: "b", status: "held", held_until: "2026-01-01T00:00:00Z" },
        { seat_id: "c", status: "sold", held_until: null },
      ],
    };

    const state = applySeatStreamMessage(createEmptySeatStreamState(), snapshot);

    expect(state.size).toBe(3);
    expect(state.get("a")).toEqual({ status: "available", held_until: null });
    expect(state.get("b")).toEqual({ status: "held", held_until: "2026-01-01T00:00:00Z" });
    expect(state.get("c")).toEqual({ status: "sold", held_until: null });
  });

  it("a diff updates exactly one seat and leaves the rest untouched", () => {
    const initial = applySeatStreamMessage(createEmptySeatStreamState(), {
      type: "snapshot",
      seats: [
        { seat_id: "a", status: "available", held_until: null },
        { seat_id: "b", status: "available", held_until: null },
      ],
    });

    const next = applySeatStreamMessage(initial, {
      type: "diff",
      seat_id: "a",
      status: "held",
      held_until: "2026-01-01T00:05:00Z",
    });

    expect(next.get("a")).toEqual({ status: "held", held_until: "2026-01-01T00:05:00Z" });
    expect(next.get("b")).toEqual({ status: "available", held_until: null });
    // original state object is untouched (immutability)
    expect(initial.get("a")).toEqual({ status: "available", held_until: null });
  });

  it("a diff for a seat id not in the current state adds it", () => {
    const state = applySeatStreamMessage(createEmptySeatStreamState(), {
      type: "diff",
      seat_id: "new-seat",
      status: "held",
      held_until: "2026-01-01T00:00:00Z",
    });
    expect(state.get("new-seat")).toEqual({ status: "held", held_until: "2026-01-01T00:00:00Z" });
  });

  it("a diff_batch applies every entry in one update", () => {
    const initial = applySeatStreamMessage(createEmptySeatStreamState(), {
      type: "snapshot",
      seats: [
        { seat_id: "a", status: "available", held_until: null },
        { seat_id: "b", status: "available", held_until: null },
        { seat_id: "c", status: "available", held_until: null },
      ],
    });

    const next = applySeatStreamMessage(initial, {
      type: "diff_batch",
      diffs: [
        { seat_id: "a", status: "held", held_until: "2026-01-01T00:05:00Z" },
        { seat_id: "b", status: "sold", held_until: null },
      ],
    });

    expect(next.get("a")).toEqual({ status: "held", held_until: "2026-01-01T00:05:00Z" });
    expect(next.get("b")).toEqual({ status: "sold", held_until: null });
    expect(next.get("c")).toEqual({ status: "available", held_until: null });
  });

  it("a realistic sequence (snapshot -> hold -> sell -> release) ends up correct", () => {
    let state = createEmptySeatStreamState();
    state = applySeatStreamMessage(state, {
      type: "snapshot",
      seats: [
        { seat_id: "s1", status: "available", held_until: null },
        { seat_id: "s2", status: "available", held_until: null },
      ],
    });
    state = applySeatStreamMessage(state, {
      type: "diff",
      seat_id: "s1",
      status: "held",
      held_until: "2026-01-01T00:05:00Z",
    });
    state = applySeatStreamMessage(state, {
      type: "diff",
      seat_id: "s1",
      status: "sold",
      held_until: null,
    });
    state = applySeatStreamMessage(state, {
      type: "diff",
      seat_id: "s2",
      status: "held",
      held_until: "2026-01-01T00:05:00Z",
    });
    state = applySeatStreamMessage(state, {
      type: "diff",
      seat_id: "s2",
      status: "available",
      held_until: null,
    });

    expect(state.get("s1")).toEqual({ status: "sold", held_until: null });
    expect(state.get("s2")).toEqual({ status: "available", held_until: null });
  });
});
