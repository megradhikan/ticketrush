import type { SeatOut } from "../types";

export interface SeatPoint {
  cx: number;
  cy: number;
}

export interface SeatLayout {
  positions: Map<string, SeatPoint>;
  seatRadius: number;
  width: number;
  height: number;
}

const PADDING = 24;
const MIN_SEAT_RADIUS = 1.25;
const MAX_SEAT_RADIUS = 9;

/**
 * Maps each seat's raw venue x/y coordinates into canvas pixel space,
 * preserving aspect ratio and centering the venue within the available
 * width/height. Pure function so it can be unit tested without a canvas.
 */
export function computeSeatLayout(
  seats: SeatOut[],
  width: number,
  height: number
): SeatLayout {
  const positions = new Map<string, SeatPoint>();

  if (seats.length === 0 || width <= 0 || height <= 0) {
    return { positions, seatRadius: MIN_SEAT_RADIUS, width, height };
  }

  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const seat of seats) {
    if (seat.x < minX) minX = seat.x;
    if (seat.x > maxX) maxX = seat.x;
    if (seat.y < minY) minY = seat.y;
    if (seat.y > maxY) maxY = seat.y;
  }

  const spanX = Math.max(maxX - minX, 1);
  const spanY = Math.max(maxY - minY, 1);
  const usableW = Math.max(width - PADDING * 2, 1);
  const usableH = Math.max(height - PADDING * 2, 1);
  const scale = Math.min(usableW / spanX, usableH / spanY);

  const offsetX = PADDING + (usableW - spanX * scale) / 2;
  const offsetY = PADDING + (usableH - spanY * scale) / 2;

  for (const seat of seats) {
    positions.set(seat.id, {
      cx: offsetX + (seat.x - minX) * scale,
      cy: offsetY + (seat.y - minY) * scale,
    });
  }

  const seatRadius = Math.min(
    Math.max(scale * 0.35, MIN_SEAT_RADIUS),
    MAX_SEAT_RADIUS
  );

  return { positions, seatRadius, width, height };
}

/**
 * Finds the seat closest to a canvas-space point, within a small hit-test
 * radius around each seat's rendered dot. Returns null if nothing is close
 * enough. O(n) per call -- fine for a single click/tap even at tens of
 * thousands of seats (this runs once per user interaction, not per frame).
 */
export function findSeatAtPoint(
  seats: SeatOut[],
  layout: SeatLayout,
  px: number,
  py: number
): SeatOut | null {
  const hitRadius = layout.seatRadius + 3;
  let closest: SeatOut | null = null;
  let closestDist = Infinity;

  for (const seat of seats) {
    const pos = layout.positions.get(seat.id);
    if (!pos) continue;
    const dx = pos.cx - px;
    const dy = pos.cy - py;
    const dist = Math.hypot(dx, dy);
    if (dist <= hitRadius && dist < closestDist) {
      closest = seat;
      closestDist = dist;
    }
  }

  return closest;
}
