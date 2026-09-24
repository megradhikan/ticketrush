# Locking strategies: Postgres-only vs Redis-first holds

PRD 5.2 asks for two interchangeable hold strategies, benchmarked side by
side. Both live behind the same function signatures and the same
`HoldError`/`ReleaseError` exceptions, selected at runtime by
`settings.hold_strategy` (`"postgres"` | `"redis"`), branched on in
`backend/app/api/routes.py`.

- **Postgres-only** (`backend/app/services/holds.py`): `SELECT ... FOR
  UPDATE SKIP LOCKED` against `seat_state`, one Postgres transaction per
  hold attempt, win or lose.
- **Redis-first** (`backend/app/services/redis_holds.py`): a Lua script
  atomically checks-and-sets `hold:{event}:{seat}` keys with a TTL in
  Redis first; only the winner then does a (still real, still legality-
  checked) Postgres write via the *same* `holds.hold_seats` function, to
  confirm the durable state. Losers never touch Postgres at all.

Full design rationale, especially why Postgres confirmation is awaited
synchronously rather than fire-and-forget, is in the module docstring at
the top of `redis_holds.py` -- not repeated here.

## Benchmark methodology

`backend/scripts/benchmark_holds.py` (not k6/Locust -- that's a separate,
general load-testing deliverable) drives both strategies with real
concurrent `asyncio.gather`'d attempts, each on its own pooled Postgres
connection, against the actually-running Postgres + Redis from
`infra/docker-compose.dev.yml` -- same "real infra, not mocks" rule the
invariant test suite follows.

Three load profiles, all buyers launched simultaneously (a genuine stampede,
not a steady trickle), each buyer targeting one seat drawn at random from a
pool:

| profile | seats | buyers | what it models |
|---|---|---|---|
| `low_contention` | 60 | 60 | inventory isn't scarce; most attempts succeed |
| `high_contention` | 15 | 60 | ~4 buyers per seat; a popular but not sold-out section |
| `single_seat_stampede` | 1 | 60 | everyone racing the literal last seat |

A warm-up phase (60 pooled Postgres connections + 60 Redis pings) runs
before any measured attempt, so cold connection/handshake cost doesn't leak
into the first profile's numbers -- that dominated an early, discarded run
(p50 >600ms) before the warm-up was added.

**Known limitation, stated plainly per the brief's "TBD, never a made-up
number" instruction:** 60 samples per profile/strategy is a small sample
for p99 specifically (p99 of 60 points is close to the max). The pattern
below reproduced consistently across multiple runs, but treat p99 as
indicative, not a tight statistical bound. Buyer count was deliberately
kept well under Postgres's `max_connections=100` because this dev instance
is shared with other in-flight branches on the same host (see
`infra/docker-compose.dev.yml`'s comment); a dedicated instance would let
this run larger. Run it yourself: `backend/.venv/bin/python
backend/scripts/benchmark_holds.py --json`.

## Measured results (one representative run, `backend/.venv`, Sept 2026)

| profile | strategy | wins/buyers | throughput (req/s) | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|---|---|---|
| low_contention | postgres | 37/60 | 2531 | 17.53 | 21.47 | 22.06 |
| low_contention | redis | 37/60 | 3679 | 13.66 | 15.63 | 15.73 |
| high_contention | postgres | 15/60 | 4140 | 10.14 | 13.62 | 13.78 |
| high_contention | redis | 15/60 | 3505 | 10.75 | 16.42 | 16.79 |
| single_seat_stampede | postgres | 1/60 | 4353 | 9.45 | 11.64 | 12.35 |
| single_seat_stampede | redis | 1/60 | 16396 | **1.10** | **1.63** | **1.70** |

Raw JSON: `backend/.venv/bin/python backend/scripts/benchmark_holds.py
--json` reproduces this table (numbers vary run to run on shared dev
hardware, but the pattern below is stable across every run taken while
writing this doc).

## What the numbers say

**The Redis-first win is proportional to how skewed demand is, not
constant.** At `low_contention` (most buyers get a seat), Redis is
modestly faster (~25% lower p50) -- both strategies do a real Postgres
write for most requests, so there's not much for Redis to shield Postgres
from. At `single_seat_stampede` (59 of 60 buyers must lose), Redis is
**~8x faster at p50 and ~4x higher throughput**, because every losing
attempt is resolved entirely inside Redis (Lua script, sub-millisecond)
and never becomes a Postgres query at all -- while every Postgres-only
attempt, win or lose, pays a full round trip: `SELECT ... FOR UPDATE SKIP
LOCKED` doesn't block a loser (that's the point of SKIP LOCKED), but it
still costs a network hop, a query plan, and a rollback. SKIP LOCKED
removes *lock contention*, not the *per-attempt Postgres round trip* --
that round trip is exactly what Redis-first eliminates for losers.

**A secondary, arguably more operationally important effect the latency
table doesn't show directly: Postgres query volume.** In
`single_seat_stampede`, the Postgres-only strategy runs 60 real
transactions against `seat_state` (59 of them for nothing); Redis-first
runs exactly 1 (the winner's confirmation). Under a real onsale stampede at
10-100x this scale, that's the difference between Postgres seeing the full
buyer-side load and Postgres seeing only the (much smaller) rate of actual
successful holds -- which is the whole reason PRD 4.2 calls Redis a
"coordination" layer, not just a cache.

**At `high_contention`, Redis's tail (p95/p99) is slightly *worse* than
Postgres-only's, not better.** This is real and worth stating rather than
smoothing over: with 15 winners out of 60, the p95/p99 percentiles land
inside the *winner* population for both strategies, and a Redis winner
pays the Lua script round trip *on top of* the same Postgres confirmation
a Postgres-only winner pays -- so winners are strictly more expensive under
Redis-first, only losers are cheaper. Whether Redis-first is a net win
depends on your win/loss ratio: great for a scarce/hot drop where most
attempts lose, roughly a wash (or a small loss at the tail) when most
attempts succeed anyway.

## The tradeoff (PRD 5.2)

- **Postgres-only** is simpler -- one code path, one datastore that has to
  agree with itself, no reconciliation logic to reason about. It's slower
  under heavy contention for the *reason measured above* (every attempt is
  a full DB round trip regardless of outcome), and it puts 100% of buyer
  load directly on Postgres.
- **Redis-first** is faster under contention and shields Postgres from
  losing-attempt volume, but it is not simpler: it introduces a second
  system that can disagree with the source of truth (PRD 4.2), and PRD 5.2
  explicitly warns against trusting Redis TTL alone to resolve that.
  `redis_holds.reconcile()` (see its module docstring) exists specifically
  to repair the two ways they can drift -- a Redis key outliving its
  Postgres row, and a Postgres row outliving its Redis key -- and is
  exercised directly in `backend/tests/test_redis_holds_invariants.py`,
  including a test that reproduces the "confirmation crashed" drift case on
  purpose and asserts the reconciler heals it without leaking the seat.

Bottom line: pick Redis-first when the product genuinely expects
stampede-shaped demand (a ticket onsale is the textbook case) and you're
willing to own reconciliation; Postgres-only is the right default when
contention is mild or operational simplicity matters more than shaving
tail latency off a losing hold attempt.
