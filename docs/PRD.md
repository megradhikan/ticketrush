# PRD: TicketRush — High-Concurrency Seat Onsale System

## 1. Summary

A ticket onsale platform that simulates a high-demand drop (think a stadium tour onsale) and proves, under real concurrency, that it never double-sells a seat, seats groups together efficiently, and can be trusted because its correctness is tested deterministically rather than just load-tested. It also includes a natural language seat search agent on top.

Built to demonstrate: correctness under concurrency, an original allocation algorithm, rigorous deterministic testing methodology, and a practical AI feature layered on a real system. Target audience: new-grad SWE / backend / platform interviews (SeatGeek and similar marketplace companies).

## 2. Goals

- Zero oversells and zero lost holds under heavy concurrent load, provable and reproducible, not just "we ran a load test once."
- A seat allocator that measurably reduces orphaned singleton seats vs naive first-come-first-served allocation, especially for group requests.
- A deterministic simulation harness that can replay any failing scenario from a seed.
- A working NL seat search agent that only returns seats that are actually currently holdable.
- Deployed, publicly runnable, with a README that documents the numbers.

## 3. Non-goals

- Real payments (mock payment provider, but with realistic latency/failure injection).
- Multi-venue marketplace features (pricing dynamics, resale, transfers). Single onsale event per run is fine.
- Full auth/account system. Simple JWT-based mock auth is enough — this is not the focus.
- Mobile clients. Web only.

## 4. System overview

### 4.1 Core entities

- **Venue**: static seat map. Sections → rows → seats. Each seat has a price tier, x/y coordinates for rendering, and section/row metadata used for adjacency (for the allocator).
- **Event**: one onsale instance tied to a venue.
- **Seat state machine**: `available -> held -> sold` and `held -> available` (on TTL expiry or explicit release). No other transitions are legal. This state machine is the thing the whole correctness story is built around.
- **Hold**: a seat reservation with a TTL (default 5 min), tied to a buyer session.
- **Order**: a set of holds converted to a sale via idempotent checkout.

### 4.2 High-level architecture

- **API layer**: FastAPI (Python).
- **Source of truth**: Postgres. Seat state lives here, not just in Redis — Redis is a performance/coordination layer, not the durable store.
- **Coordination**: Redis for waiting-room queue, hold TTLs, and distributed locking primitives (Lua scripts for atomic check-and-hold).
- **Realtime**: WebSocket layer pushing seat map diffs (seat X went from available to held) to connected clients. This reuses the broadcast/pub-sub pattern from the Parallel project (Redis pub/sub fanout to WS connections).
- **Frontend**: React + TypeScript. Interactive seat map (canvas or SVG, not DOM-per-seat at scale — 20k seats will die in the DOM) rendered from venue JSON, with live availability.
- **Deployment**: Docker Compose locally; AWS (ECS/Fargate + RDS Postgres + ElastiCache Redis) for the deployed demo.
- **Load/chaos testing**: k6 or Locust for load; a custom deterministic simulator (see section 6) for correctness.

## 5. Feature 1 — Correctness core (holds, waiting room, idempotent checkout)

This is table stakes but has to be airtight since everything else is built on it.

### 5.1 Waiting room
- Buyers hit a queue endpoint on entering the onsale. Assigned a position + estimated admit time.
- Admission happens in batches (configurable batch size + interval) to cap concurrent load on the seat-selection API.
- Queue position stored in Redis (sorted set, score = arrival timestamp).
- Admitted buyers get a short-lived admission token required to call seat-hold endpoints.

### 5.2 Seat holds
- Hold = atomic operation: check seat is `available` in Postgres AND acquire a Redis lock/TTL key for that seat, or fail. Use a Lua script so the check-and-set is atomic in Redis; use `SELECT ... FOR UPDATE SKIP LOCKED` (or equivalent) on the Postgres side for the durable state transition.
- Implement and benchmark **two strategies** side by side — this is an explicit deliverable, not just an implementation detail:
  1. Postgres-only: `SELECT ... FOR UPDATE SKIP LOCKED` row locking.
  2. Redis-first: Lua atomic hold in Redis, async-confirmed in Postgres.
- Compare p50/p99 hold latency and throughput between the two under the same load profile. Write up the tradeoff (Redis is faster but needs careful reconciliation logic if Redis and Postgres disagree; Postgres-only is simpler but slower under contention).
- Holds expire via Redis TTL. On expiry, a background reconciler releases the seat back to `available` in Postgres (don't rely on Redis TTL alone as the source of truth — that's a correctness bug waiting to happen).

### 5.3 Idempotent checkout
- Checkout takes an idempotency key (client-generated UUID). Duplicate requests with the same key return the original result, never double-charge or double-sell.
- Checkout converts a set of `held` seats (must all still belong to this buyer's session and not be expired) to `sold` in a single Postgres transaction.
- Simulate payment latency and a configurable failure rate. On payment failure, seats return to `available` (or stay held briefly for retry — your call, but document the choice).

### 5.4 Correctness invariants (must hold under all conditions — this is the whole point)
1. No seat is ever `sold` more than once.
2. No seat is `held` by two different sessions simultaneously.
3. Every `held` seat either transitions to `sold` or eventually returns to `available` (no permanent leaks).
4. Checkout is idempotent: N identical requests produce exactly 1 order.

## 6. Feature 2 — Deterministic simulation testing (novelty #1)

Standard load testing tells you "it handled load." It doesn't tell you *why* it broke when it broke, or let you reproduce that exact failure. Build a simulation harness instead.

### 6.1 Design
- A simulator that generates a **seeded** sequence of events: buyer arrivals, seat selections, holds, checkouts, expirations, and injected faults (Redis connection drop, Postgres slow query, worker crash mid-transaction, network delay on payment callback).
- Given the same seed, the simulator produces the exact same interleaving every time. This is the key property — a bug found in run #4821 is reproducible by rerunning seed 4821, not "try to hit it again and hope."
- After each simulated run, check all 4 invariants from 5.4 against the final DB state and the full event log.
- Run thousands of seeds in CI (or a batch job). Any invariant violation gets logged with its seed, the offending sequence of events, and the exact seats/sessions involved.
- This mirrors a fault-injection fuzzer approach (seeded RNG, run log, invariant checker as a separate pass over the log) — the same mental model used to find real bugs in other correctness-critical systems.

### 6.2 Deliverables
- A CLI: `simulate --seed 4821 --buyers 5000 --fault-rate 0.02`
- A report: seeds run, invariant violations found (should converge to 0 as bugs get fixed — document the ones you *did* find and fix, that's a great interview story), and a replay mode for any failing seed.
- This is arguably the single most differentiating piece of the whole project. Most candidates load test. Almost none build a reproducible fault-injection simulator. Don't undersell this in the README.

## 7. Feature 3 — Best-available allocator (novelty #2)

Naive first-come-first-served seat selection leaves the venue fragmented: singles and pairs scattered everywhere, groups of 4+ unable to sit together even when 20-30% of the venue is unsold.

### 7.1 Behavior
- Buyer requests N seats with constraints: max budget per seat, price tier(s) acceptable, "together" required or not.
- Allocator searches for a contiguous (or near-contiguous, configurable adjacency tolerance) run of N available seats in a single row, within constraints, minimizing fragmentation cost.
- Fragmentation cost function: penalize allocations that leave orphaned singles/pairs adjacent to the newly held block. This needs to run fast under concurrency, so:
  - Maintain a per-row availability bitmap/interval structure (not a full scan of the seats table) for fast contiguous-run lookup.
  - Since this runs concurrently with holds/releases from other buyers, the allocator has to work against the same atomic hold mechanism from section 5.2, not a stale snapshot. Reasonable approach: compute candidate seats fast from the bitmap, then attempt the atomic hold on the candidate set, retry with next-best candidate on conflict.

### 7.2 Metrics to report (this is the deliverable, not just the algorithm)
- Orphan rate: % of unsold seats that are singles/pairs with no adjacent unsold seat, measured at various stages of sellout (25%, 50%, 75%, 90% sold).
- Group satisfaction rate: % of group requests (size ≥ 2) seated fully contiguous, vs naive random/FCFS allocation on the same simulated demand.
- Revenue impact: naive allocation that fragments the venue effectively "strands" inventory; estimate $ value of stranded seats at high sellout percentages under naive vs your allocator.
- Run this comparison through the section 6 simulator so it's reproducible and stated with real numbers, not vibes.

## 8. Feature 4 — Natural language seat search (novelty #3, AI layer)

A conversational layer over the live seat map. Buyer types something like "4 seats together, aisle, under $150 each, good view of the stage" and gets back seats that are actually holdable right now.

### 8.1 Design
- Small agent (tool-calling LLM, e.g. via Claude API or Groq/Llama depending on cost preference) with tools over:
  - `search_seats(criteria)` — hits the same allocator/availability logic from section 7, not a separate stale index. This is the important constraint: the agent must never suggest a seat that's actually held/sold. Query live state, always.
  - `get_seat_details(seat_ids)` — price, view quality tag, section/row.
- Parse constraints from natural language into the structured query the allocator already understands (budget, count, together, section/tier preference, "aisle" as a seat attribute if tagged in the venue JSON).
- Response includes the seats found plus a short natural-language explanation ("these 4 are together in Section 112, row H, aisle, avg $142/seat").
- Keep this thin. It's a UX layer over real systems work, not the core of the project. Scope it to 1-2 weeks max.

### 8.2 Guardrail (important, and a good interview talking point)
- Every seat the agent returns must be re-validated against live availability at hold time, same as any other buyer. The agent has no special path that bypasses the atomic hold logic. If a race happens (seat got taken between search and hold), the agent falls back to next-best from its last query, transparently.

## 9. Data model (rough)

```
venues(id, name, layout_json)
events(id, venue_id, name, onsale_start_at, status)
seats(id, venue_id, section, row, seat_number, price_tier, x, y, attributes jsonb)
seat_state(seat_id, event_id, status enum[available,held,sold], held_by_session, held_until, version)
sessions(id, admitted_at, admission_token)
orders(id, session_id, idempotency_key unique, status, created_at)
order_seats(order_id, seat_id)
sim_runs(seed, params jsonb, invariant_violations jsonb, run_at)
```

`seat_state.version` for optimistic locking / detecting stale reads if needed.

## 10. API surface (rough)

```
POST /queue/join                  -> { position, est_admit_at }
GET  /queue/status                -> { position, admitted: bool, admission_token? }
GET  /events/:id/seats            -> live seat map state (for initial render)
WS   /events/:id/stream           -> seat state diffs
POST /seats/hold                  { seat_ids | search_criteria } -> { held_seats, expires_at }
POST /seats/release               { seat_ids }
POST /checkout                    { seat_ids, idempotency_key } -> { order }
POST /search/nl                   { query text } -> { candidate_seats, explanation }
```

## 11. Testing & metrics summary (what goes in the README / resume bullet)

- Load: sustained X concurrent buyers, p50/p99 hold + checkout latency, across both locking strategies (5.2).
- Correctness: N simulation runs, 0 invariant violations (or: found and fixed K violations — document them).
- Allocation: orphan rate and group-satisfaction rate, allocator vs naive, at multiple sellout percentages.
- NL search: query -> valid holdable seats success rate, plus latency.

## 12. Suggested build order (phases)

1. **Phase 1 — core correctness**: venue model, Postgres seat state machine, basic hold/release/checkout (single locking strategy first, Postgres-only), simple seat map UI, no waiting room yet. Get invariants 1-4 provably true with a basic test suite.
2. **Phase 2 — concurrency + waiting room**: Redis holds, waiting room, the second locking strategy, load testing with k6/Locust, WebSocket live updates.
3. **Phase 3 — simulation harness**: seeded simulator, fault injection, invariant checker, replay mode. Run it against phase 1/2 to shake out real bugs.
4. **Phase 4 — allocator**: best-available algorithm, fragmentation metrics, run through the simulator for the comparison numbers.
5. **Phase 5 — NL search agent**: thin layer on top, guardrail validation.
6. **Phase 6 — deploy + polish**: AWS deployment, README with all metrics and graphs, demo video/GIF of the seat map under live load.

Phases 1-3 are the core story and should not be rushed. Phases 4-5 are the differentiators but are much less valuable without a provably correct foundation under them.

## 13. Stack

- Backend: Python, FastAPI
- Datastores: Postgres, Redis
- Frontend: React, TypeScript, WebSockets, canvas/SVG seat rendering
- Infra: Docker, AWS (ECS/Fargate, RDS, ElastiCache), Gitlab or GitHub for version control
- Load testing: k6 or Locust
- LLM layer: Claude API or Groq/Llama (tool-calling agent) for section 8
