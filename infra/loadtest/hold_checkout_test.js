// Load test for the REST hold/checkout endpoints (PRD section 11:
// "sustained X concurrent buyers, p50/p99 hold + checkout latency").
// Not a WS test -- it drives POST /sessions, POST .../seats/hold, and
// POST .../checkout the same way a real buyer's browser would.
//
// Each iteration: create a buyer session, hold one seat, then check out
// that seat with a fresh idempotency key. Seats are assigned by a global,
// monotonically increasing iteration counter (k6/execution's
// scenario.iterationInTest) indexing into the full seat list seeded by
// seed_event.py, so concurrent VUs never race for the same seat by
// construction -- contention in these numbers comes from real system load
// (DB row locks, connection pool, event loop), not from every VU fighting
// over seat #1.
//
// Usage (see README.md for the full setup):
//   k6 run -e BASE_URL=http://localhost:8000 -e EVENT_ID=<uuid> \
//        -e VUS=50 -e DURATION=30s infra/loadtest/hold_checkout_test.js

import http from 'k6/http';
import { check } from 'k6';
import { Trend, Rate } from 'k6/metrics';
import { SharedArray } from 'k6/data';
import { scenario } from 'k6/execution';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const EVENT_ID = __ENV.EVENT_ID || open('./seed_data/event_id.txt').trim();
const VUS = Number(__ENV.VUS || 50);
const DURATION = __ENV.DURATION || '30s';
const RAMP = __ENV.RAMP || '5s';

const seatIds = new SharedArray('seats', function () {
  return JSON.parse(open('./seed_data/seats.json'));
});

// Small dependency-free uuidv4: this only needs to be unique per iteration
// for idempotency-key purposes, not cryptographically secure, so no need
// to pull in an external jslib over the network at load-test time.
function uuidv4() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

export const options = {
  scenarios: {
    onsale: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: RAMP, target: VUS },
        { duration: DURATION, target: VUS },
        { duration: RAMP, target: 0 },
      ],
      gracefulRampDown: '10s',
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

const holdDuration = new Trend('hold_duration', true);
const checkoutDuration = new Trend('checkout_duration', true);
const holdFailureRate = new Rate('hold_failure_rate');
const checkoutFailureRate = new Rate('checkout_failure_rate');

export default function () {
  const seatId = seatIds[scenario.iterationInTest % seatIds.length];

  const sessionRes = http.post(`${BASE_URL}/sessions`, null, {
    tags: { name: 'create_session' },
  });
  if (!check(sessionRes, { 'session created': (r) => r.status === 200 })) {
    return;
  }
  const sessionId = sessionRes.json('id');

  const holdRes = http.post(
    `${BASE_URL}/events/${EVENT_ID}/seats/hold`,
    JSON.stringify({ session_id: sessionId, seat_ids: [seatId] }),
    { headers: { 'Content-Type': 'application/json' }, tags: { name: 'hold' } }
  );
  holdDuration.add(holdRes.timings.duration);
  const held = holdRes.status === 200;
  holdFailureRate.add(!held);
  if (!held) {
    return;
  }

  const checkoutRes = http.post(
    `${BASE_URL}/events/${EVENT_ID}/checkout`,
    JSON.stringify({
      session_id: sessionId,
      seat_ids: [seatId],
      idempotency_key: uuidv4(),
    }),
    { headers: { 'Content-Type': 'application/json' }, tags: { name: 'checkout' } }
  );
  checkoutDuration.add(checkoutRes.timings.duration);
  checkoutFailureRate.add(checkoutRes.status !== 200);
}
