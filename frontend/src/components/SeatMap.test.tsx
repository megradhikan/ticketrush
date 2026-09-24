import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { computeSeatLayout } from "../lib/seatMapGeometry";
import type { SeatOut } from "../types";
import { SeatMap } from "./SeatMap";

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

// jsdom doesn't implement 2d canvas rendering. SeatMap only needs getContext
// to return *something* with the drawing methods it calls -- stub it so the
// component's draw effect runs without throwing, and so click hit-testing
// (which doesn't touch the context at all) can be exercised for real.
function stubCanvasContext() {
  const ctx = {
    setTransform: vi.fn(),
    fillRect: vi.fn(),
    beginPath: vi.fn(),
    arc: vi.fn(),
    fill: vi.fn(),
    fillStyle: "",
  } as unknown as CanvasRenderingContext2D;
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(ctx);
  vi.spyOn(HTMLCanvasElement.prototype, "getBoundingClientRect").mockReturnValue({
    x: 0,
    y: 0,
    left: 0,
    top: 0,
    right: 400,
    bottom: 300,
    width: 400,
    height: 300,
    toJSON: () => {},
  });
}

beforeEach(() => {
  stubCanvasContext();
});

describe("SeatMap", () => {
  it("renders a single canvas element (not one DOM node per seat)", () => {
    const seats = Array.from({ length: 50 }, (_, i) => seat(`s${i}`, i, 0));
    render(
      <SeatMap seats={seats} selectedIds={new Set()} heldByMeIds={new Set()} width={400} height={300} onSeatClick={() => {}} />
    );
    const canvases = document.querySelectorAll("canvas");
    expect(canvases.length).toBe(1);
    expect(screen.getByTestId("seat-map-canvas").tagName).toBe("CANVAS");
  });

  it("clicking on an available seat's rendered position calls onSeatClick with that seat", () => {
    const seats = [seat("a", 0, 0), seat("b", 100, 0), seat("c", 0, 100)];
    const onSeatClick = vi.fn();
    render(
      <SeatMap seats={seats} selectedIds={new Set()} heldByMeIds={new Set()} width={400} height={300} onSeatClick={onSeatClick} />
    );

    // Click exactly where computeSeatLayout (the same pure function SeatMap
    // uses internally) places seat "b".
    const layout = computeSeatLayout(seats, 400, 300);
    const pos = layout.positions.get("b")!;
    const canvas = screen.getByTestId("seat-map-canvas");
    fireEvent.click(canvas, { clientX: pos.cx, clientY: pos.cy });

    expect(onSeatClick).toHaveBeenCalledTimes(1);
    expect(onSeatClick.mock.calls[0][0].id).toBe("b");
  });

  it("clicking empty space does not call onSeatClick", () => {
    const seats = [seat("a", 0, 0)];
    const onSeatClick = vi.fn();
    render(
      <SeatMap seats={seats} selectedIds={new Set()} heldByMeIds={new Set()} width={400} height={300} onSeatClick={onSeatClick} />
    );
    const canvas = screen.getByTestId("seat-map-canvas");
    fireEvent.click(canvas, { clientX: 399, clientY: 299 });
    expect(onSeatClick).not.toHaveBeenCalled();
  });
});
