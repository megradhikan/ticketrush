# Load test results (PRD section 11)

k6 script: `infra/loadtest/hold_checkout_test.js`. Measures the REST
hold/checkout path only (`POST /sessions` -> `POST .../seats/hold` ->
`POST .../checkout`), not the WebSocket layer. Run instructions:
`infra/loadtest/README.md`.

## Methodology

- Backend: this branch (`phase2-websocket-fanout`), Postgres-only hold
  strategy (`HOLD_STRATEGY=postgres`, Phase 1's `SELECT ... FOR UPDATE SKIP
  LOCKED` path) -- the Redis-first strategy from PRD 5.2 is being built on a
  separate parallel branch and isn't merged yet, so it isn't part of this
  comparison. This run is the "Postgres-only" half of the eventual
  side-by-side; the Redis-first numbers are **TBD** until that branch lands.
- Ran against an **isolated** Postgres 16 + Redis 7 (fresh Docker
  containers on non-default ports, migrated with `alembic upgrade head`),
  not the shared `infra/docker-compose.dev.yml` stack. That stack is shared
  across every worktree/subagent working on this repo right now, and its
  tests `TRUNCATE` every table before each test run -- a first attempt at
  this load test against the shared DB got its 20k-seat pool wiped mid-run
  by an unrelated concurrent test run (confirmed via row counts before/
  after). Isolating the load test's own DB avoids that entirely and is a
  fairer measurement anyway (no test-suite noise sharing the same Postgres
  connections).
- Hardware: single MacBook (Apple Silicon), backend (`uvicorn`, no
  `--workers`, one process), Postgres, Redis, and k6 all running locally on
  the same machine -- so these numbers include the machine's own noise
  floor (all four sharing CPU/IO), not a clean multi-host benchmark. Treat
  them as directionally real, not authoritative.
- `checkout_payment_latency_ms=150`, `checkout_payment_failure_rate=0.0`
  (repo defaults) -- so checkout latency is dominated by the simulated
  150ms payment call, on top of the actual DB work.
- Seed: 20,000 seats (`infra/loadtest/seed_event.py`, 4 sections x 100 rows
  x 50 seats), so every hold+checkout in the run consumes a fresh,
  never-before-touched seat (indexed by k6's global `scenario.iterationInTest`
  counter) -- 409 conflicts in the numbers below are genuine contention on
  the seat's row lock, session, etc., not two VUs colliding on the same
  seat by construction.

## Run: 50 sustained concurrent buyers, 30s

```
k6 run -e VUS=50 -e DURATION=30s -e RAMP=5s hold_checkout_test.js
```

5s ramp-up -> 30s at 50 VUs -> 5s ramp-down. 7,926 completed hold+checkout
iterations, 0 failed HTTP checks.

| Metric | p50 (med) | p90 | p95 | p99 | max | avg |
|---|---|---|---|---|---|---|
| **Hold latency** | 12.7ms | 23.8ms | 29.2ms | 44.1ms | 82.2ms | 14.6ms |
| **Checkout latency** | 178.1ms | 198.8ms | 210.0ms | 239.9ms | 349.8ms | 181.6ms |

- Throughput: **197.9 iterations/s** (one hold + one checkout each) =
  ~198 successful sales/sec sustained, 593.6 HTTP req/s total (3 requests
  per iteration: session, hold, checkout).
- `hold_failure_rate`: 0.00% (0 / 7,926) -- no seat contention 409s at this
  concurrency, as expected since each VU targets a distinct seat.
- `checkout_failure_rate`: 0.00% (0 / 7,926).
- Checkout's p50 of ~178ms sits just above the 150ms simulated payment
  floor; hold's p50 of ~12.7ms is real row-lock + commit overhead on top of
  a single `SELECT ... FOR UPDATE SKIP LOCKED` + `UPDATE`.

Raw k6 summary output for this run is preserved below for reference:

```
checks_total.......: 7926    197.878204/s
checks_succeeded...: 100.00% 7926 out of 7926
checks_failed......: 0.00%   0 out of 7926

hold_duration..................: avg=14.57ms  min=1.67ms  med=12.74ms  p(90)=23.76ms  p(95)=29.18ms  p(99)=44.06ms  max=82.17ms
hold_failure_rate..............: 0.00%  0 out of 7926
checkout_duration..............: avg=181.61ms min=153.54ms med=178.1ms  p(90)=198.79ms p(95)=210.04ms p(99)=239.94ms max=349.81ms
checkout_failure_rate..........: 0.00%  0 out of 7926

http_req_duration..............: avg=73.72ms  min=1.67ms  med=24.28ms  p(90)=184.17ms p(95)=193.18ms p(99)=218.43ms max=349.81ms
http_req_failed................: 0.00%  0 out of 23778
http_reqs......................: 23778  593.634613/s
iterations.....................: 7926   197.878204/s
```

## What's TBD

- **Redis-first strategy comparison** (PRD 5.2's explicit side-by-side):
  blocked on the parallel `phase2-redis-holds` branch merging. Once it's
  in, rerun this exact script unchanged against `HOLD_STRATEGY=redis` and
  add the second column here.
- **Multi-process / multi-worker backend**: this run used a single
  `uvicorn` process (no `--workers`), matching how the realtime fanout
  (`app.services.realtime`) was primarily being validated for correctness,
  not peak throughput. Numbers under `uvicorn --workers N` behind a real
  load balancer would look different (and are the more representative
  "production" shape) -- not measured here.
- **Higher concurrency** (100s-1000s of VUs, closer to a real onsale
  stampede): not run in this pass: constrained to what's reasonable to
  drive from a single laptop sharing CPU with Postgres/Redis/k6 itself.
  `-e VUS=<n>` on the same script scales this up whenever it's run
  somewhere with more headroom.
