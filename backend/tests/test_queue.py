"""Waiting room (PRD 5.1): position assignment, batch admission, and status
reporting, against real Postgres + Redis (join order via ZRANK is exactly
the kind of thing an in-memory fake would get subtly wrong under
concurrency)."""

import asyncio
import uuid

import pytest

from app.services import queue as queue_service


async def test_join_assigns_increasing_positions_in_arrival_order(venue_with_seats, make_session, db, redis_client):
    _, event, _ = venue_with_seats
    session_ids = [await make_session() for _ in range(5)]

    positions = []
    for sid in session_ids:
        result = await queue_service.join_queue(db, redis_client, event.id, sid, batch_size=100, batch_interval_seconds=2.0)
        positions.append(result.position)

    assert positions == [1, 2, 3, 4, 5]


async def test_join_is_idempotent_keeps_original_position(venue_with_seats, make_session, db, redis_client):
    _, event, _ = venue_with_seats
    first = await make_session()
    second = await make_session()

    r1 = await queue_service.join_queue(db, redis_client, event.id, first, batch_size=100, batch_interval_seconds=2.0)
    await queue_service.join_queue(db, redis_client, event.id, second, batch_size=100, batch_interval_seconds=2.0)
    # first re-joins -- should NOT be bumped behind second.
    r1_again = await queue_service.join_queue(db, redis_client, event.id, first, batch_size=100, batch_interval_seconds=2.0)

    assert r1.position == 1
    assert r1_again.position == 1


async def test_join_concurrent_arrivals_get_distinct_positions(new_db_session, venue_with_seats, make_session, redis_client):
    """Real concurrency: N buyers join at "the same time" -- every position
    1..N must be assigned exactly once, no duplicates, no gaps. Each
    concurrent join uses its own Postgres session/connection (new_db_session)
    -- a single AsyncSession isn't safe to drive from multiple concurrent
    coroutines, same rule test_invariants.py follows."""
    _, event, _ = venue_with_seats
    n = 20
    session_ids = await asyncio.gather(*(make_session() for _ in range(n)))

    async def join(sid: uuid.UUID) -> int:
        s = new_db_session()
        r = await queue_service.join_queue(s, redis_client, event.id, sid, batch_size=100, batch_interval_seconds=2.0)
        return r.position

    positions = await asyncio.gather(*(join(sid) for sid in session_ids))
    assert sorted(positions) == list(range(1, n + 1))


async def test_admit_next_batch_admits_front_of_queue_only(venue_with_seats, make_session, db, redis_client):
    _, event, _ = venue_with_seats
    session_ids = [await make_session() for _ in range(5)]
    for sid in session_ids:
        await queue_service.join_queue(db, redis_client, event.id, sid, batch_size=2, batch_interval_seconds=2.0)

    admitted = await queue_service.admit_next_batch(db, redis_client, event.id, batch_size=2)
    assert admitted == 2

    statuses = [await queue_service.get_status(db, redis_client, event.id, sid) for sid in session_ids]
    admitted_flags = [s.admitted for s in statuses]
    assert admitted_flags == [True, True, False, False, False], statuses

    # Admitted buyers get a token; the rest don't, and keep a live position.
    assert all(s.admission_token for s in statuses[:2])
    assert statuses[2].position == 1
    assert statuses[3].position == 2
    assert statuses[4].position == 3


async def test_admit_next_batch_empty_queue_is_noop(venue_with_seats, db, redis_client):
    _, event, _ = venue_with_seats
    admitted = await queue_service.admit_next_batch(db, redis_client, event.id, batch_size=10)
    assert admitted == 0


async def test_status_for_already_admitted_session_reports_zero_position(venue_with_seats, make_session, db, redis_client):
    _, event, _ = venue_with_seats
    sid = await make_session()
    await queue_service.join_queue(db, redis_client, event.id, sid, batch_size=1, batch_interval_seconds=2.0)
    await queue_service.admit_next_batch(db, redis_client, event.id, batch_size=1)

    status = await queue_service.get_status(db, redis_client, event.id, sid)
    assert status.admitted is True
    assert status.position == 0
    assert status.admission_token is not None


async def test_join_unknown_session_raises(venue_with_seats, db, redis_client):
    _, event, _ = venue_with_seats
    with pytest.raises(ValueError):
        await queue_service.join_queue(db, redis_client, event.id, uuid.uuid4(), batch_size=10, batch_interval_seconds=2.0)


async def test_admit_all_due_events_covers_multiple_events(db, redis_client, make_session):
    from app.db.seed import create_venue_with_seats

    _, event1, _ = await create_venue_with_seats(db, name="Venue 1", sections=1, rows_per_section=1, seats_per_row=2)
    _, event2, _ = await create_venue_with_seats(db, name="Venue 2", sections=1, rows_per_section=1, seats_per_row=2)

    s1 = await make_session()
    s2 = await make_session()
    await queue_service.join_queue(db, redis_client, event1.id, s1, batch_size=10, batch_interval_seconds=2.0)
    await queue_service.join_queue(db, redis_client, event2.id, s2, batch_size=10, batch_interval_seconds=2.0)

    results = await queue_service.admit_all_due_events(db, redis_client, batch_size=10)
    assert results.get(str(event1.id)) == 1
    assert results.get(str(event2.id)) == 1

    status1 = await queue_service.get_status(db, redis_client, event1.id, s1)
    status2 = await queue_service.get_status(db, redis_client, event2.id, s2)
    assert status1.admitted and status2.admitted
