# TicketRush frontend (Phase 2 -- buyer seat map)

Canvas-rendered seat map + buyer flow (select -> hold -> checkout) against the
Phase 1 REST API, plus a WebSocket client for live seat diffs built against a
local mock (the real `GET /events/{event_id}/stream` endpoint is being built
in parallel on another branch). See `docs/PRD.md` sections 4.2 and 10 for the
full spec this implements.

## Setup

```bash
npm install
cp .env.example .env.local
```

Edit `.env.local` and set `VITE_EVENT_ID` to a real event id. Get one by
seeding the backend (from `backend/`, with its venv active):

```bash
python3 -c "
import asyncio
from app.db.session import AsyncSessionLocal
from app.db.seed import create_venue_with_seats
async def main():
    async with AsyncSessionLocal() as db:
        venue, event, seats = await create_venue_with_seats(db, sections=4, rows_per_section=15, seats_per_row=30)
        print('EVENT_ID', event.id)
asyncio.run(main())
"
```

Then:

```bash
npm run dev
```

The dev server proxies `/sessions` and `/events/*` to `http://localhost:8000`
(see `vite.config.ts`) so the browser never needs backend CORS support. If
your backend is running on a different port (8000 was already taken by
something else on this machine when this was built), run Vite with
`BACKEND_ORIGIN=http://localhost:<port> npm run dev`.

## Testing

```bash
npm run test
```

Covers, at minimum:
- `src/lib/seatStreamReducer.test.ts` -- the WS diff reducer: given a
  snapshot + a sequence of diffs/diff_batches, does seat state end up
  correct.
- `src/lib/seatMapGeometry.test.ts` -- canvas layout math and click
  hit-testing (pure functions, no canvas needed).
- `src/lib/selection.test.ts` -- seat selection toggling and the
  hold-conflict (409) seat-removal logic.
- `src/components/SeatMap.test.tsx` -- the canvas component renders exactly
  one `<canvas>` regardless of seat count, and clicking a seat's rendered
  position invokes the click callback with the right seat.

## Architecture notes

- **Canvas, not DOM-per-seat.** `src/components/SeatMap.tsx` draws every seat
  with `ctx.arc` in a single effect; there is exactly one `<canvas>` element
  no matter how many seats are in the venue. Click hit-testing
  (`src/lib/seatMapGeometry.ts`) is a plain O(n) scan over seat positions,
  but that only runs once per click, not once per frame, so it stays cheap
  even at tens of thousands of seats.
- **Buyer flow** (`src/components/BuyerFlow.tsx`): creates a session, fetches
  the seat map, lets the user multi-select available seats, holds them, then
  checks out with a fresh `crypto.randomUUID()` idempotency key (reused
  across retries of the *same* checkout attempt, so retrying after a network
  error is still idempotent). A 409 on hold reports which seats were grabbed
  by someone else; those are deselected and the user is told, rather than
  the request just failing silently.

### WebSocket client for live diffs -- what to flip when the real server exists

The frozen message contract (`src/lib/seatStreamReducer.ts`) is:

```
{"type": "snapshot", "seats": [{"seat_id", "status", "held_until"}, ...]}
{"type": "diff", "seat_id", "status", "held_until"}
{"type": "diff_batch", "diffs": [{"seat_id", "status", "held_until"}, ...]}
```

`src/lib/seatStreamTransport.ts` picks a transport based on a URL: anything
starting with `mock://` uses an in-memory mock that sends a synthetic
snapshot then periodic random diffs; anything else opens a real
`WebSocket(url)` and parses incoming JSON against the same contract. The
`useSeatStream` hook (`src/hooks/useSeatStream.ts`) just calls
`resolveSeatStreamUrl(eventId)` and doesn't know or care which transport it
gets.

**To point this at the real server once it exists**, set one env var in
`frontend/.env.local`:

```
VITE_WS_URL=ws://localhost:8000/events/{eventId}/stream
```

`{eventId}` is substituted for the live event id. That's the entire
integration step -- no component, hook, or reducer code changes. Leaving
`VITE_WS_URL` unset keeps using the mock.

Known mock limitation (documented, not a bug): the mock has no visibility
into real hold/checkout calls made through the REST API -- it just invents
random diffs for seats the local buyer isn't currently selecting/holding
(via `getLocallyOwnedIds`). The real server derives diffs from the same DB
the REST endpoints write to, so this asymmetry disappears once you switch to
it.
