/** Mirrors backend/app/api/schemas.py. Keep in sync with the live API. */

export type SeatStatus = "available" | "held" | "sold";

export interface SeatOut {
  id: string;
  section: string;
  row: string;
  seat_number: number;
  price_tier: string;
  x: number;
  y: number;
  attributes: Record<string, unknown>;
  status: SeatStatus;
  held_until: string | null;
}

export interface SessionOut {
  id: string;
}

export interface HoldResponse {
  held_seats: string[];
  expires_at: string;
}

export interface ReleaseResponse {
  released_seats: string[];
}

export interface OrderOut {
  id: string;
  status: string;
  seat_ids: string[];
  created_at: string;
}

export interface CheckoutResponse {
  order: OrderOut;
}

/** 409 response body from POST /events/{id}/seats/hold */
export interface HoldConflictDetail {
  unavailable_seat_ids: string[];
}

/** 409 response body from POST /events/{id}/checkout */
export interface CheckoutConflictDetail {
  seats_not_held: string[];
}
