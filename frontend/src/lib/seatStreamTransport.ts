import type { SeatOut, SeatStatus } from "../types";
import type { SeatStreamMessage } from "./seatStreamReducer";

export interface SeatStreamHandlers {
  onMessage: (message: SeatStreamMessage) => void;
  onOpen?: () => void;
  onClose?: () => void;
  onError?: (error: unknown) => void;
}

export interface SeatStreamTransport {
  close: () => void;
}

/**
 * ============================================================================
 * HOW TO POINT THIS AT THE REAL WS SERVER
 * ============================================================================
 * Right now GET /events/{event_id}/stream doesn't exist yet -- it's being
 * built in parallel on a separate branch. This file talks to a local mock
 * instead (see createMockSeatStreamTransport below), which speaks the exact
 * same frozen message contract (see seatStreamReducer.ts) that the real
 * server will.
 *
 * Once the real server exists, integration is a ONE-LINE config change, not
 * a code change: set VITE_WS_URL in frontend/.env(.local), e.g.
 *
 *   VITE_WS_URL=ws://localhost:8000/events/{eventId}/stream
 *
 * `{eventId}` is substituted for the live event id at connect time. Any
 * value here that isn't "mock://..." is treated as a real WebSocket URL and
 * routed through createWebSocketSeatStreamTransport. Leaving VITE_WS_URL
 * unset keeps using the mock. No component or hook needs to change.
 * ============================================================================
 */
export function resolveSeatStreamUrl(eventId: string): string {
  const template = import.meta.env.VITE_WS_URL as string | undefined;
  if (template && template.trim().length > 0) {
    return template.replace("{eventId}", eventId);
  }
  return `mock://seat-stream/${eventId}`;
}

export interface CreateSeatStreamOptions {
  /** Initial seats, used only by the mock transport to synthesize a snapshot
   * and to know which seat ids it's allowed to invent chaos for. */
  seedSeats?: SeatOut[];
  /** Called by the mock transport on each tick to find out which seat ids
   * are currently selected/held by the local buyer, so the mock avoids
   * generating conflicting diffs for seats the user is actively interacting
   * with. The real server has no such concept -- it just reports what's
   * actually true in the DB -- this is purely a mock-quality-of-life knob. */
  getLocallyOwnedIds?: () => ReadonlySet<string>;
}

export function createSeatStreamTransport(
  url: string,
  handlers: SeatStreamHandlers,
  options: CreateSeatStreamOptions = {}
): SeatStreamTransport {
  if (url.startsWith("mock://")) {
    return createMockSeatStreamTransport(handlers, options);
  }
  return createWebSocketSeatStreamTransport(url, handlers);
}

function createWebSocketSeatStreamTransport(
  url: string,
  handlers: SeatStreamHandlers
): SeatStreamTransport {
  const socket = new WebSocket(url);

  socket.onopen = () => handlers.onOpen?.();
  socket.onclose = () => handlers.onClose?.();
  socket.onerror = (event) => handlers.onError?.(event);
  socket.onmessage = (event) => {
    try {
      const parsed = JSON.parse(event.data as string) as SeatStreamMessage;
      handlers.onMessage(parsed);
    } catch (err) {
      handlers.onError?.(err);
    }
  };

  return {
    close: () => socket.close(),
  };
}

const MOCK_TICK_MS = 4000;
const STATUS_ROLL: SeatStatus[] = ["held", "held", "available", "sold"];

/**
 * Local stand-in for the real WS server described in the frozen contract.
 * Sends a snapshot right after "connecting" (matching real connect timing),
 * then periodically emits synthetic diff / diff_batch messages for a few
 * random seats so the live-update path (a seat flips color in the UI when a
 * diff arrives) can be built and tested without the real server.
 *
 * Known limitation (acceptable for a mock, documented so it isn't a
 * surprise): this mock has no idea what actually happened via the REST hold/
 * checkout endpoints -- the real server derives diffs from the same DB those
 * endpoints write to, this one just invents random chaos. `getLocallyOwnedIds`
 * is used to at least avoid overwriting seats the local buyer currently has
 * selected or held, so a demo session doesn't fight itself.
 */
function createMockSeatStreamTransport(
  handlers: SeatStreamHandlers,
  options: CreateSeatStreamOptions
): SeatStreamTransport {
  const seedSeats = options.seedSeats ?? [];
  const allSeatIds = seedSeats.map((s) => s.id);
  let closed = false;

  const openTimer = setTimeout(() => {
    if (closed) return;
    handlers.onOpen?.();
    handlers.onMessage({
      type: "snapshot",
      seats: seedSeats.map((s) => ({
        seat_id: s.id,
        status: s.status,
        held_until: s.held_until,
      })),
    });
  }, 0);

  const interval = setInterval(() => {
    if (closed || allSeatIds.length === 0) return;
    const owned = options.getLocallyOwnedIds?.() ?? new Set<string>();
    const candidates = allSeatIds.filter((id) => !owned.has(id));
    if (candidates.length === 0) return;

    const batchSize = Math.min(1 + Math.floor(Math.random() * 3), candidates.length);
    const diffs: Array<{ seat_id: string; status: SeatStatus; held_until: string | null }> = [];
    const picked = new Set<string>();
    while (picked.size < batchSize) {
      const id = candidates[Math.floor(Math.random() * candidates.length)];
      if (picked.has(id)) continue;
      picked.add(id);
      const status = STATUS_ROLL[Math.floor(Math.random() * STATUS_ROLL.length)];
      diffs.push({
        seat_id: id,
        status,
        held_until: status === "held" ? new Date(Date.now() + 45_000).toISOString() : null,
      });
    }

    if (diffs.length === 1) {
      handlers.onMessage({ type: "diff", ...diffs[0] });
    } else {
      handlers.onMessage({ type: "diff_batch", diffs });
    }
  }, MOCK_TICK_MS);

  return {
    close: () => {
      if (closed) return;
      closed = true;
      clearTimeout(openTimer);
      clearInterval(interval);
      handlers.onClose?.();
    },
  };
}
