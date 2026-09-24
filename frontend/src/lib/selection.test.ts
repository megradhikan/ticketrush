import { describe, expect, it } from "vitest";

import type { SeatOut } from "../types";
import { removeUnavailableFromSelection, toggleSeatSelection } from "./selection";

function seat(id: string, status: SeatOut["status"]): SeatOut {
  return {
    id,
    section: "Section 1",
    row: "A",
    seat_number: 1,
    price_tier: "GA",
    x: 0,
    y: 0,
    attributes: {},
    status,
    held_until: null,
  };
}

describe("toggleSeatSelection", () => {
  it("adds an available seat that isn't yet selected", () => {
    const next = toggleSeatSelection(new Set(), seat("a", "available"));
    expect(next.has("a")).toBe(true);
  });

  it("removes an available seat that is already selected", () => {
    const next = toggleSeatSelection(new Set(["a"]), seat("a", "available"));
    expect(next.has("a")).toBe(false);
  });

  it("does nothing when the seat is held", () => {
    const selected = new Set(["a"]);
    const next = toggleSeatSelection(selected, seat("b", "held"));
    expect(next.has("b")).toBe(false);
    expect(Array.from(next)).toEqual(["a"]);
  });

  it("does nothing when the seat is sold", () => {
    const next = toggleSeatSelection(new Set(), seat("a", "sold"));
    expect(next.has("a")).toBe(false);
  });

  it("does not mutate the input set", () => {
    const original = new Set(["x"]);
    toggleSeatSelection(original, seat("y", "available"));
    expect(Array.from(original)).toEqual(["x"]);
  });
});

describe("removeUnavailableFromSelection", () => {
  it("removes only the ids reported as unavailable", () => {
    const selected = new Set(["a", "b", "c"]);
    const { next, removed } = removeUnavailableFromSelection(selected, ["b"]);
    expect(Array.from(next).sort()).toEqual(["a", "c"]);
    expect(removed).toEqual(["b"]);
  });

  it("ignores unavailable ids that were never selected", () => {
    const selected = new Set(["a"]);
    const { next, removed } = removeUnavailableFromSelection(selected, ["z"]);
    expect(Array.from(next)).toEqual(["a"]);
    expect(removed).toEqual([]);
  });

  it("does not mutate the input set", () => {
    const original = new Set(["a", "b"]);
    removeUnavailableFromSelection(original, ["a"]);
    expect(Array.from(original).sort()).toEqual(["a", "b"]);
  });
});
