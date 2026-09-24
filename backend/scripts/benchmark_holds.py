"""PRD 5.2 / 11 explicit deliverable: benchmark the two hold strategies
(services/holds.py Postgres-only, services/redis_holds.py Redis-first)
side by side under the same load profile and report p50/p99 hold latency
and throughput for each.

Not k6/Locust (that's a separate general load-testing deliverable per the
brief) -- this is a small, purpose-built asyncio driver that exercises
exactly the code path under test (hold_seats), the same way the invariant
tests do: real concurrent coroutines, each with its own Postgres connection
via the session factory, against the real Postgres + Redis from
infra/docker-compose.dev.yml.

Load profile: `--buyers` concurrent buyers each attempt to hold ONE seat
drawn (with repetition) from a pool of `--seats` seats, all launched via
asyncio.gather so they genuinely race each other in the same instant --
this is the realistic "hot onsale" shape the PRD is about (many buyers
scrambling for a limited pool, not `buyers` buyers each calmly holding a
seat nobody else wants). A LOW_CONTENTION profile (seats ~= buyers, i.e.
inventory isn't scarce) and a HIGH_CONTENTION profile (seats << buyers) are
both run by default so the writeup isn't relying on a single cherry-picked
shape.

Usage:
    backend/.venv/bin/python backend/scripts/benchmark_holds.py
    backend/.venv/bin/python backend/scripts/benchmark_holds.py --buyers 500 --seats 500 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from redis.asyncio import from_url  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.seed import create_venue_with_seats  # noqa: E402
from app.models.buyer_session import BuyerSession  # noqa: E402
from app.services import holds as holds_service  # noqa: E402
from app.services import redis_holds  # noqa: E402

settings = get_settings()


@dataclass
class RunResult:
    strategy: str
    profile: str
    n_buyers: int
    n_seats: int
    n_successful_holds: int
    wall_seconds: float
    throughput_req_per_sec: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(round(pct / 100 * (len(sorted_values) - 1))))
    return sorted_values[idx]


async def _setup(
    engine, session_factory, redis, n_seats: int, n_buyers: int
) -> tuple[uuid.UUID, list[uuid.UUID], list[uuid.UUID]]:
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "TRUNCATE seat_state, order_seats, orders, seats, events, venues, sessions, sim_runs CASCADE"
        )
    if redis is not None:
        await redis.flushdb()

    rows = max(1, min(26, (n_seats + 49) // 50))
    per_row = max(1, -(-n_seats // rows))  # ceil
    async with session_factory() as db:
        _, event, seats = await create_venue_with_seats(
            db, name="Benchmark Arena", sections=1, rows_per_section=rows, seats_per_row=per_row
        )
        seat_ids = [s.id for s in seats[:n_seats]]
        buyer_sessions = [BuyerSession(id=uuid.uuid4()) for _ in range(n_buyers)]
        db.add_all(buyer_sessions)
        await db.commit()
        session_ids = [b.id for b in buyer_sessions]

    return event.id, seat_ids, session_ids


async def _run_postgres(
    session_factory, event_id: uuid.UUID, seat_ids: list[uuid.UUID], session_ids: list[uuid.UUID], ttl: int
) -> tuple[list[float], int, float]:
    rng = random.Random(42)
    targets = [rng.choice(seat_ids) for _ in session_ids]

    async def attempt(session_id: uuid.UUID, seat_id: uuid.UUID) -> tuple[float, bool]:
        async with session_factory() as db:
            t0 = time.perf_counter()
            try:
                await holds_service.hold_seats(db, event_id, session_id, [seat_id], ttl)
                ok = True
            except holds_service.HoldError:
                ok = False
            return (time.perf_counter() - t0) * 1000, ok

    start = time.perf_counter()
    results = await asyncio.gather(*(attempt(sid, seat) for sid, seat in zip(session_ids, targets, strict=True)))
    wall = time.perf_counter() - start
    latencies = [r[0] for r in results]
    successes = sum(1 for r in results if r[1])
    return latencies, successes, wall


async def _run_redis(
    session_factory,
    redis,
    event_id: uuid.UUID,
    seat_ids: list[uuid.UUID],
    session_ids: list[uuid.UUID],
    ttl: int,
) -> tuple[list[float], int, float]:
    rng = random.Random(42)
    targets = [rng.choice(seat_ids) for _ in session_ids]

    async def attempt(session_id: uuid.UUID, seat_id: uuid.UUID) -> tuple[float, bool]:
        async with session_factory() as db:
            t0 = time.perf_counter()
            try:
                await redis_holds.hold_seats(db, redis, event_id, session_id, [seat_id], ttl)
                ok = True
            except redis_holds.HoldError:
                ok = False
            return (time.perf_counter() - t0) * 1000, ok

    start = time.perf_counter()
    results = await asyncio.gather(*(attempt(sid, seat) for sid, seat in zip(session_ids, targets, strict=True)))
    wall = time.perf_counter() - start
    latencies = [r[0] for r in results]
    successes = sum(1 for r in results if r[1])
    return latencies, successes, wall


async def _bench_one(
    engine, session_factory, redis, strategy: str, profile: str, n_seats: int, n_buyers: int, ttl: int
) -> RunResult:
    event_id, seat_ids, session_ids = await _setup(engine, session_factory, redis, n_seats, n_buyers)

    if strategy == "postgres":
        latencies, successes, wall = await _run_postgres(session_factory, event_id, seat_ids, session_ids, ttl)
    else:
        latencies, successes, wall = await _run_redis(session_factory, redis, event_id, seat_ids, session_ids, ttl)

    latencies.sort()
    return RunResult(
        strategy=strategy,
        profile=profile,
        n_buyers=n_buyers,
        n_seats=n_seats,
        n_successful_holds=successes,
        wall_seconds=round(wall, 4),
        throughput_req_per_sec=round(n_buyers / wall, 1) if wall > 0 else 0.0,
        p50_ms=round(_percentile(latencies, 50), 2),
        p95_ms=round(_percentile(latencies, 95), 2),
        p99_ms=round(_percentile(latencies, 99), 2),
        max_ms=round(latencies[-1], 2) if latencies else 0.0,
    )


async def _warm_up(session_factory, redis, n: int) -> None:
    """Opens/exercises `n` pooled Postgres connections and `n` Redis
    commands before any measured run, so TCP/auth handshake cost for a cold
    connection pool doesn't leak into the first profile's latency numbers
    (it otherwise dominates: opening 60 fresh Postgres connections at once
    is far slower than any single hold_seats call)."""

    async def touch_pg() -> None:
        async with session_factory() as db:
            await db.execute(text("SELECT 1"))

    async def touch_redis() -> None:
        await redis.ping()

    await asyncio.gather(*(touch_pg() for _ in range(n)))
    await asyncio.gather(*(touch_redis() for _ in range(n)))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buyers", type=int, default=None, help="override buyer count for both profiles")
    parser.add_argument("--seats", type=int, default=None, help="override seat count for both profiles")
    parser.add_argument("--ttl", type=int, default=300)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON instead of a table")
    args = parser.parse_args()

    # pool_size sized to the largest buyer count we'll run, so the
    # Postgres-only numbers reflect FOR UPDATE SKIP LOCKED contention
    # itself, not artificial queueing for a connection-pool slot that a
    # differently-configured production pool wouldn't have.
    # Kept well under Postgres's default max_connections=100 -- this repo's
    # dev Postgres is shared with other in-flight branches on the same host
    # (see infra/docker-compose.dev.yml), so the benchmark deliberately
    # doesn't try to claim the whole connection budget. Override with
    # --buyers/--seats for a larger run against a dedicated instance.
    buyers = args.buyers or 60
    profiles = [
        ("low_contention", args.seats or buyers, buyers),  # ~1 buyer/seat: inventory isn't scarce
        ("high_contention", args.seats or max(1, buyers // 4), buyers),  # ~4 buyers/seat
        ("single_seat_stampede", args.seats or 1, buyers),  # everyone racing the same last seat
    ]
    max_buyers = max(b for _, _, b in profiles)

    engine = create_async_engine(settings.database_url, pool_size=max_buyers, max_overflow=10)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = from_url(settings.redis_url, decode_responses=True)

    results: list[RunResult] = []
    try:
        await _warm_up(session_factory, redis, max_buyers)
        for profile, n_seats, n_buyers in profiles:
            for strategy in ("postgres", "redis"):
                r = await _bench_one(engine, session_factory, redis, strategy, profile, n_seats, n_buyers, args.ttl)
                results.append(r)
    finally:
        await engine.dispose()
        await redis.aclose()

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
        return

    header = (
        f"{'profile':<16} {'strategy':<10} {'buyers':>7} {'seats':>6} {'wins':>6} "
        f"{'throughput/s':>13} {'p50 ms':>8} {'p95 ms':>8} {'p99 ms':>8} {'max ms':>8}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r.profile:<16} {r.strategy:<10} {r.n_buyers:>7} {r.n_seats:>6} {r.n_successful_holds:>6} "
            f"{r.throughput_req_per_sec:>13.1f} {r.p50_ms:>8.2f} {r.p95_ms:>8.2f} {r.p99_ms:>8.2f} {r.max_ms:>8.2f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
