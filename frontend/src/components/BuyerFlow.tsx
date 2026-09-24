import { useEffect, useMemo, useRef, useState } from "react";

import { ApiError, checkout as checkoutApi, createSession, getEventSeats, holdSeats } from "../api/client";
import { useSeatStream } from "../hooks/useSeatStream";
import { mergeSeatStatuses } from "../lib/mergeSeatStatuses";
import { removeUnavailableFromSelection, toggleSeatSelection } from "../lib/selection";
import type {
  CheckoutConflictDetail,
  HoldConflictDetail,
  OrderOut,
  SeatOut,
  SessionOut,
} from "../types";
import { SeatMap } from "./SeatMap";

type LoadPhase = "loading" | "error" | "ready";

interface Banner {
  kind: "error" | "info";
  text: string;
}

export interface BuyerFlowProps {
  eventId: string;
}

export function BuyerFlow({ eventId }: BuyerFlowProps) {
  const [phase, setPhase] = useState<LoadPhase>("loading");
  const [session, setSession] = useState<SessionOut | null>(null);
  const [seats, setSeats] = useState<SeatOut[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [heldIds, setHeldIds] = useState<Set<string>>(new Set());
  const [holdExpiresAt, setHoldExpiresAt] = useState<string | null>(null);
  const [checkoutKey, setCheckoutKey] = useState<string | null>(null);
  const [order, setOrder] = useState<OrderOut | null>(null);
  const [banner, setBanner] = useState<Banner | null>(null);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  // Seats the local buyer currently cares about, so the WS mock doesn't
  // invent conflicting chaos for them (see seatStreamTransport.ts).
  const locallyOwnedRef = useRef<ReadonlySet<string>>(new Set());
  useEffect(() => {
    locallyOwnedRef.current = new Set([...selectedIds, ...heldIds]);
  }, [selectedIds, heldIds]);

  const { liveStatus, connected } = useSeatStream(eventId, seats, locallyOwnedRef);
  const mergedSeats = useMemo(() => mergeSeatStatuses(seats, liveStatus), [seats, liveStatus]);

  // Initial load: mock buyer session + venue/seat snapshot.
  useEffect(() => {
    let cancelled = false;
    async function init() {
      try {
        const [sessionRes, seatsRes] = await Promise.all([createSession(), getEventSeats(eventId)]);
        if (cancelled) return;
        setSession(sessionRes);
        setSeats(seatsRes);
        setPhase("ready");
      } catch {
        if (!cancelled) {
          setPhase("error");
          setBanner({
            kind: "error",
            text: "Could not load the seat map. Is the backend running and is VITE_EVENT_ID set to a real event?",
          });
        }
      }
    }
    init();
    return () => {
      cancelled = true;
    };
  }, [eventId]);

  // Live countdown tick for the hold TTL.
  useEffect(() => {
    if (!holdExpiresAt) return;
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [holdExpiresAt]);

  // React to live diffs: drop selected seats someone else just took, and
  // release seats whose hold got reported as expired/available again.
  useEffect(() => {
    if (liveStatus.size === 0) return;

    setSelectedIds((prev) => {
      if (prev.size === 0) return prev;
      const takenIds: string[] = [];
      for (const id of prev) {
        const entry = liveStatus.get(id);
        if (entry && entry.status !== "available") takenIds.push(id);
      }
      if (takenIds.length === 0) return prev;
      const { next, removed } = removeUnavailableFromSelection(prev, takenIds);
      setBanner({
        kind: "error",
        text: `${removed.length} selected seat${removed.length === 1 ? "" : "s"} ${
          removed.length === 1 ? "was" : "were"
        } just taken by someone else and ${removed.length === 1 ? "has" : "have"} been deselected.`,
      });
      return next;
    });

    setHeldIds((prevHeld) => {
      if (prevHeld.size === 0) return prevHeld;
      const stillHeld = new Set(prevHeld);
      let expiredAny = false;
      for (const id of prevHeld) {
        const entry = liveStatus.get(id);
        if (entry && entry.status === "available") {
          stillHeld.delete(id);
          expiredAny = true;
        }
      }
      if (!expiredAny) return prevHeld;
      if (stillHeld.size === 0) {
        setHoldExpiresAt(null);
        setCheckoutKey(null);
        setBanner({ kind: "error", text: "Your hold expired before checkout. Please reselect your seats." });
      }
      return stillHeld;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveStatus]);

  const remainingSeconds = holdExpiresAt
    ? Math.max(0, Math.round((new Date(holdExpiresAt).getTime() - now) / 1000))
    : null;

  useEffect(() => {
    if (remainingSeconds === 0 && heldIds.size > 0) {
      setHeldIds(new Set());
      setHoldExpiresAt(null);
      setCheckoutKey(null);
      setBanner({ kind: "error", text: "Your hold expired. Please reselect your seats." });
    }
  }, [remainingSeconds, heldIds.size]);

  function handleSeatClick(seat: SeatOut) {
    if (order || busy) return;
    if (heldIds.size > 0) {
      setBanner({ kind: "info", text: "You already have seats held. Checkout or wait for the hold to expire before selecting more." });
      return;
    }
    if (seat.status !== "available") {
      setBanner({ kind: "info", text: "That seat isn't available." });
      return;
    }
    setBanner(null);
    setSelectedIds((prev) => toggleSeatSelection(prev, seat));
  }

  async function handleHold() {
    if (!session || selectedIds.size === 0 || busy) return;
    setBusy(true);
    setBanner(null);
    try {
      const res = await holdSeats(eventId, session.id, Array.from(selectedIds));
      setHeldIds(new Set(res.held_seats));
      setHoldExpiresAt(res.expires_at);
      setSelectedIds(new Set());
      setCheckoutKey(crypto.randomUUID());
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const detail = err.detail as HoldConflictDetail | undefined;
        const unavailable = detail?.unavailable_seat_ids ?? [];
        const { next, removed } = removeUnavailableFromSelection(selectedIds, unavailable);
        setSelectedIds(next);
        setBanner({
          kind: "error",
          text:
            removed.length > 0
              ? `${removed.length} seat${removed.length === 1 ? "" : "s"} ${
                  removed.length === 1 ? "was" : "were"
                } just grabbed by someone else and ${removed.length === 1 ? "has" : "have"} been deselected. Review your selection and try again.`
              : "Some of the selected seats are no longer available.",
        });
      } else {
        setBanner({ kind: "error", text: "Could not hold seats right now. Please try again." });
      }
    } finally {
      setBusy(false);
    }
  }

  async function handleCheckout() {
    if (!session || heldIds.size === 0 || !checkoutKey || busy) return;
    setBusy(true);
    setBanner(null);
    try {
      const res = await checkoutApi(eventId, session.id, Array.from(heldIds), checkoutKey);
      setOrder(res.order);
      setHeldIds(new Set());
      setHoldExpiresAt(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const detail = err.detail as CheckoutConflictDetail | undefined;
        setBanner({
          kind: "error",
          text:
            detail && detail.seats_not_held.length > 0
              ? "Checkout failed: your hold on some of these seats expired. Please reselect."
              : "Checkout failed: these seats are no longer held by you. Please reselect.",
        });
        setHeldIds(new Set());
        setHoldExpiresAt(null);
        setCheckoutKey(null);
      } else {
        setBanner({
          kind: "error",
          text: "Checkout failed. It's safe to retry -- the same request won't be double-charged.",
        });
      }
    } finally {
      setBusy(false);
    }
  }

  function handleStartOver() {
    setOrder(null);
    setSelectedIds(new Set());
    setHeldIds(new Set());
    setHoldExpiresAt(null);
    setCheckoutKey(null);
    setBanner(null);
  }

  if (phase === "loading") {
    return <p className="status-line">Loading seat map...</p>;
  }

  if (phase === "error") {
    return <p className="status-line status-line--error">{banner?.text ?? "Failed to load."}</p>;
  }

  return (
    <div className="buyer-flow">
      <div className="buyer-flow__toolbar">
        <Legend />
        <div className="buyer-flow__conn">
          <span className={`conn-dot ${connected ? "conn-dot--on" : "conn-dot--off"}`} />
          {connected ? "Live updates connected" : "Connecting live updates..."}
        </div>
      </div>

      {banner && <div className={`banner banner--${banner.kind}`}>{banner.text}</div>}

      <SeatMap
        seats={mergedSeats}
        selectedIds={selectedIds}
        heldByMeIds={heldIds}
        onSeatClick={handleSeatClick}
      />

      <div className="buyer-flow__actions">
        {!order && heldIds.size === 0 && (
          <>
            <span className="buyer-flow__summary">{selectedIds.size} seat{selectedIds.size === 1 ? "" : "s"} selected</span>
            <button disabled={selectedIds.size === 0 || busy} onClick={handleHold}>
              {busy ? "Holding..." : `Hold ${selectedIds.size || ""} seat${selectedIds.size === 1 ? "" : "s"}`}
            </button>
          </>
        )}

        {!order && heldIds.size > 0 && (
          <>
            <span className="buyer-flow__summary">
              Holding {heldIds.size} seat{heldIds.size === 1 ? "" : "s"}
              {remainingSeconds !== null && ` -- expires in ${formatSeconds(remainingSeconds)}`}
            </span>
            <button disabled={busy} onClick={handleCheckout}>
              {busy ? "Checking out..." : "Checkout"}
            </button>
          </>
        )}

        {order && (
          <div className="order-confirmation">
            <p>
              Order <code>{order.id}</code> confirmed ({order.status}) -- {order.seat_ids.length} seat
              {order.seat_ids.length === 1 ? "" : "s"}.
            </p>
            <button onClick={handleStartOver}>Start over</button>
          </div>
        )}
      </div>
    </div>
  );
}

function formatSeconds(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function Legend() {
  const items: Array<[string, string]> = [
    ["#3a4152", "Available"],
    ["#22c55e", "Selected"],
    ["#3b82f6", "Held by you"],
    ["#e0a72e", "Held by others"],
    ["#565b66", "Sold"],
  ];
  return (
    <div className="legend">
      {items.map(([color, label]) => (
        <span className="legend__item" key={label}>
          <span className="legend__swatch" style={{ backgroundColor: color }} />
          {label}
        </span>
      ))}
    </div>
  );
}
