import type {
  CheckoutResponse,
  HoldResponse,
  ReleaseResponse,
  SeatOut,
  SessionOut,
} from "../types";

/**
 * Base URL for the REST API. Left empty by default so requests are relative
 * and go through the Vite dev server proxy configured in vite.config.ts
 * (avoids needing CORS middleware on the backend for local dev). Set
 * VITE_API_BASE_URL to point at a different backend (e.g. a deployed one).
 */
const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    super(`API request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });

  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      // response had no JSON body; leave detail as null
    }
    const detail =
      body && typeof body === "object" && "detail" in body
        ? (body as { detail: unknown }).detail
        : body;
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) {
    return undefined as T;
  }
  return (await res.json()) as T;
}

export function createSession(): Promise<SessionOut> {
  return request<SessionOut>("/sessions", { method: "POST" });
}

export function getEventSeats(eventId: string): Promise<SeatOut[]> {
  return request<SeatOut[]>(`/events/${eventId}/seats`);
}

export function holdSeats(
  eventId: string,
  sessionId: string,
  seatIds: string[]
): Promise<HoldResponse> {
  return request<HoldResponse>(`/events/${eventId}/seats/hold`, {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId, seat_ids: seatIds }),
  });
}

export function releaseSeats(
  eventId: string,
  sessionId: string,
  seatIds: string[]
): Promise<ReleaseResponse> {
  return request<ReleaseResponse>(`/events/${eventId}/seats/release`, {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId, seat_ids: seatIds }),
  });
}

export function checkout(
  eventId: string,
  sessionId: string,
  seatIds: string[],
  idempotencyKey: string
): Promise<CheckoutResponse> {
  return request<CheckoutResponse>(`/events/${eventId}/checkout`, {
    method: "POST",
    body: JSON.stringify({
      session_id: sessionId,
      seat_ids: seatIds,
      idempotency_key: idempotencyKey,
    }),
  });
}
