import { describe, expect, it } from "vitest";

import type { SeatOut } from "../types";
import { computeSeatLayout, findSeatAtPoint } from "./seatMapGeometry";

function seat(id: string, x: number, y: number, status: SeatOut["status"] = "available"): SeatOut {
  return {
    id,
    section: "Section 1",
    row: "A",
    seat_number: 1,
    price_tier: "GA",
    x,
    y,
    attributes: {},
    status,
    held_until: null,
  };
}

describe("computeSeatLayout", () => {
  it("maps every seat id to a position", () => {
    const seats = [seat("a", 0, 0), seat("b", 10, 0), seat("c", 0, 10)];
    const layout = computeSeatLayout(seats, 400, 300);
    expect(layout.positions.size).toBe(3);
    for (const s of seats) {
      expect(layout.positions.has(s.id)).toBe(true);
    }
  });

  it("keeps all seats within the canvas bounds (plus a small radius margin)", () => {
    const seats = [seat("a", 0, 0), seat("b", 100, 0), seat("c", 0, 50), seat("d", 100, 50)];
    const layout = computeSeatLayout(seats, 500, 400);
    for (const s of seats) {
      const pos = layout.positions.get(s.id)!;
      expect(pos.cx).toBeGreaterThanOrEqual(0);
      expect(pos.cx).toBeLessThanOrEqual(500);
      expect(pos.cy).toBeGreaterThanOrEqual(0);
      expect(pos.cy).toBeLessThanOrEqual(400);
    }
  });

  it("handles an empty seat list without throwing", () => {
    const layout = computeSeatLayout([], 400, 300);
    expect(layout.positions.size).toBe(0);
  });

  it("a seat further right in venue space renders further right on canvas", () => {
    const seats = [seat("left", 0, 0), seat("right", 100, 0)];
    const layout = computeSeatLayout(seats, 400, 300);
    const left = layout.positions.get("left")!;
    const right = layout.positions.get("right")!;
    expect(right.cx).toBeGreaterThan(left.cx);
  });
});

describe("findSeatAtPoint", () => {
  it("finds the seat under a click at its exact rendered position", () => {
    const seats = [seat("a", 0, 0), seat("b", 100, 0), seat("c", 0, 100)];
    const layout = computeSeatLayout(seats, 400, 300);
    const pos = layout.positions.get("b")!;
    const found = findSeatAtPoint(seats, layout, pos.cx, pos.cy);
    expect(found?.id).toBe("b");
  });

  it("returns null when the click is far from every seat", () => {
    const seats = [seat("a", 0, 0), seat("b", 100, 0)];
    const layout = computeSeatLayout(seats, 400, 300);
    const found = findSeatAtPoint(seats, layout, 9999, 9999);
    expect(found).toBeNull();
  });

  it("picks the closest seat when two seats are near the click", () => {
    const seats = [seat("near", 10, 10), seat("far", 90, 90)];
    const layout = computeSeatLayout(seats, 400, 300);
    const nearPos = layout.positions.get("near")!;
    const found = findSeatAtPoint(seats, layout, nearPos.cx + 0.5, nearPos.cy + 0.5);
    expect(found?.id).toBe("near");
  });
});
