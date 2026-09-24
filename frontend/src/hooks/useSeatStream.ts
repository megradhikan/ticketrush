import { useEffect, useRef, useState } from "react";

import {
  applySeatStreamMessage,
  createEmptySeatStreamState,
  type SeatStreamState,
} from "../lib/seatStreamReducer";
import { createSeatStreamTransport, resolveSeatStreamUrl } from "../lib/seatStreamTransport";
import type { SeatOut } from "../types";

/**
 * Owns the live seat-stream connection for one event: connects (real WS or
 * mock, see seatStreamTransport.ts for how that's chosen), feeds every
 * inbound message through the pure reducer, and exposes the latest known
 * status per seat.
 *
 * `seedSeats` should be the seat list from the initial REST fetch -- used to
 * seed the mock's synthetic snapshot/diffs. `excludedIdsRef` is optional and
 * lets the mock avoid clobbering seats the local buyer currently has
 * selected/held (see seatStreamTransport.ts doc comment); pass a ref so
 * selection changes don't force a reconnect.
 */
export function useSeatStream(
  eventId: string | null,
  seedSeats: SeatOut[],
  excludedIdsRef?: React.MutableRefObject<ReadonlySet<string>>
) {
  const [liveStatus, setLiveStatus] = useState<SeatStreamState>(createEmptySeatStreamState);
  const [connected, setConnected] = useState(false);
  const seedRef = useRef(seedSeats);
  seedRef.current = seedSeats;

  const hasSeeds = seedSeats.length > 0;

  useEffect(() => {
    if (!eventId || !hasSeeds) return;

    const url = resolveSeatStreamUrl(eventId);
    const transport = createSeatStreamTransport(
      url,
      {
        onOpen: () => setConnected(true),
        onClose: () => setConnected(false),
        onError: () => setConnected(false),
        onMessage: (message) => setLiveStatus((prev) => applySeatStreamMessage(prev, message)),
      },
      {
        seedSeats: seedRef.current,
        getLocallyOwnedIds: excludedIdsRef ? () => excludedIdsRef.current : undefined,
      }
    );

    return () => transport.close();
    // Reconnect only when the event changes, or when we transition from "no
    // seed data yet" to "have seed data" -- not on every seat list refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eventId, hasSeeds]);

  return { liveStatus, connected };
}
