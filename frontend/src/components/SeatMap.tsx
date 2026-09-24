import { useEffect, useRef } from "react";

import { computeSeatLayout, findSeatAtPoint, type SeatLayout } from "../lib/seatMapGeometry";
import type { SeatOut } from "../types";

const COLOR_AVAILABLE = "#3a4152";
const COLOR_SELECTED = "#22c55e";
const COLOR_HELD_MINE = "#3b82f6";
const COLOR_HELD_OTHER = "#e0a72e";
const COLOR_SOLD = "#565b66";
const COLOR_BACKGROUND = "#12141a";

export interface SeatMapProps {
  seats: SeatOut[];
  selectedIds: ReadonlySet<string>;
  heldByMeIds: ReadonlySet<string>;
  width?: number;
  height?: number;
  onSeatClick: (seat: SeatOut) => void;
}

/**
 * Renders the venue as a single HTML canvas -- one draw call per seat, not
 * one DOM node per seat. At a few thousand seats this redraws in a couple of
 * milliseconds; the PRD's concern (20k seats killing the DOM) doesn't apply
 * here because there's no DOM per seat to kill.
 *
 * Hit-testing on click is a plain O(n) scan over seat positions (see
 * findSeatAtPoint) -- that's a single scan per user click, not per frame, so
 * it stays cheap even at tens of thousands of seats.
 */
export function SeatMap({
  seats,
  selectedIds,
  heldByMeIds,
  width = 900,
  height = 560,
  onSeatClick,
}: SeatMapProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const layoutRef = useRef<SeatLayout | null>(null);

  useEffect(() => {
    const layout = computeSeatLayout(seats, width, height);
    layoutRef.current = layout;

    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    ctx.fillStyle = COLOR_BACKGROUND;
    ctx.fillRect(0, 0, width, height);

    for (const seat of seats) {
      const pos = layout.positions.get(seat.id);
      if (!pos) continue;

      let color = COLOR_AVAILABLE;
      if (seat.status === "sold") {
        color = COLOR_SOLD;
      } else if (seat.status === "held") {
        color = heldByMeIds.has(seat.id) ? COLOR_HELD_MINE : COLOR_HELD_OTHER;
      } else if (selectedIds.has(seat.id)) {
        color = COLOR_SELECTED;
      }

      ctx.beginPath();
      ctx.fillStyle = color;
      ctx.arc(pos.cx, pos.cy, layout.seatRadius, 0, Math.PI * 2);
      ctx.fill();
    }
  }, [seats, selectedIds, heldByMeIds, width, height]);

  function handleClick(event: React.MouseEvent<HTMLCanvasElement>) {
    const layout = layoutRef.current;
    const canvas = canvasRef.current;
    if (!layout || !canvas) return;
    const rect = canvas.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const py = event.clientY - rect.top;
    const seat = findSeatAtPoint(seats, layout, px, py);
    if (seat) onSeatClick(seat);
  }

  return (
    <canvas
      ref={canvasRef}
      onClick={handleClick}
      role="img"
      aria-label="Venue seat map"
      data-testid="seat-map-canvas"
      style={{ cursor: "pointer", borderRadius: 8, display: "block" }}
    />
  );
}
