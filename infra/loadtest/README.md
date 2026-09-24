# Load testing (PRD section 11)

k6 script driving the REST hold/checkout endpoints under configurable
concurrency: `POST /sessions` -> `POST /events/:id/seats/hold` -> `POST
/events/:id/checkout`, measuring p50/p90/p95/p99 latency for hold and
checkout separately. Not a WebSocket test -- that's covered by
`backend/tests/test_realtime.py`.

## 1. Install k6

```
brew install k6      # macOS
```
(or see https://k6.io/docs/get-started/installation/ for other platforms)

## 2. Start Postgres + Redis and the backend

```
docker compose -f infra/docker-compose.dev.yml up -d   # from repo root
cd backend
.venv/bin/alembic upgrade head        # if not already migrated
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 3. Seed a large event to sell through

Each seat can only be held+sold once, so seed enough seats to comfortably
cover the run (defaults to 20,000 -- see seed_event.py's docstring for the
sizing rationale):

```
cd backend
.venv/bin/python ../infra/loadtest/seed_event.py
```

This writes `infra/loadtest/seed_data/event_id.txt` and `seats.json`, which
the k6 script reads by default.

## 4. Run it

```
cd infra/loadtest
k6 run -e VUS=50 -e DURATION=30s hold_checkout_test.js
```

Env vars (all optional):
- `BASE_URL` -- default `http://localhost:8000`
- `EVENT_ID` -- default: read from `seed_data/event_id.txt`
- `VUS` -- sustained concurrent virtual buyers, default `50`
- `DURATION` -- sustained-load duration after ramp-up, default `30s`
- `RAMP` -- ramp-up/ramp-down duration, default `5s`

k6's summary output reports `hold_duration` and `checkout_duration` custom
trends with `med` (p50), `p(95)`, and `p(99)`, plus `hold_failure_rate` /
`checkout_failure_rate` (409s from seat contention or a seed pool that ran
out mid-run). Re-seed (step 3) before re-running once the pool is
exhausted -- sold seats don't come back.

Results from an actual run against this branch are recorded in
`docs/load-test-results.md`.
