"""Waiting room (PRD 5.1). Buyers join a per-event Redis sorted set on
arrival (score = arrival timestamp), get a live position from ZRANK, and are
admitted in batches by a periodic background sweep (see
app.workers.background.admission_loop) that pops the front of the queue and
issues each admitted buyer a short-lived admission token, persisted onto
their BuyerSession row in Postgres.

Deliberately NOT wired as a hard gate in front of the Phase 1 hold/checkout
endpoints -- see `require_admission` below and the note in
app/api/routes.py. This module implements the feature end-to-end and tests
it as one; whether it's enforced is a separate switch.
"""

import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.buyer_session import BuyerSession

# All queue members for an event that have never been popped by a batch yet.
_QUEUE_EVENTS_SET = "queue_events"


def _queue_key(event_id: uuid.UUID) -> str:
    return f"queue:{event_id}"


@dataclass
class QueueJoinResult:
    position: int
    est_admit_at: datetime


@dataclass
class QueueStatusResult:
    position: int
    admitted: bool
    admission_token: str | None


async def join_queue(
    db: AsyncSession,
    redis: Redis,
    event_id: uuid.UUID,
    session_id: uuid.UUID,
    batch_size: int,
    batch_interval_seconds: float,
) -> QueueJoinResult:
    session = await db.get(BuyerSession, session_id)
    if session is None:
        raise ValueError(f"no such session: {session_id}")

    if session.admitted_at is not None:
        # Already admitted (e.g. rejoining after a page refresh) -- no
        # position, already through.
        return QueueJoinResult(position=0, est_admit_at=datetime.now(UTC))

    key = _queue_key(event_id)
    # NX: a buyer who calls /queue/join twice keeps their original arrival
    # timestamp/position rather than getting bumped to the back of the line.
    await redis.zadd(key, {str(session_id): time.time()}, nx=True)
    await redis.sadd(_QUEUE_EVENTS_SET, str(event_id))

    rank = await redis.zrank(key, str(session_id))
    position = (rank if rank is not None else 0) + 1

    batches_ahead = (position - 1) // batch_size
    est_admit_at = datetime.now(UTC) + timedelta(seconds=batches_ahead * batch_interval_seconds + batch_interval_seconds)

    return QueueJoinResult(position=position, est_admit_at=est_admit_at)


async def get_status(
    db: AsyncSession,
    redis: Redis,
    event_id: uuid.UUID,
    session_id: uuid.UUID,
) -> QueueStatusResult:
    session = await db.get(BuyerSession, session_id)
    if session is None:
        raise ValueError(f"no such session: {session_id}")

    if session.admitted_at is not None:
        return QueueStatusResult(position=0, admitted=True, admission_token=session.admission_token)

    rank = await redis.zrank(_queue_key(event_id), str(session_id))
    position = (rank + 1) if rank is not None else 0
    return QueueStatusResult(position=position, admitted=False, admission_token=None)


async def admit_next_batch(db: AsyncSession, redis: Redis, event_id: uuid.UUID, batch_size: int) -> int:
    """Pop up to `batch_size` buyers off the front of the queue and admit
    them: issue each a short-lived admission token and persist it onto their
    BuyerSession row in Postgres (the durable record of admission -- Redis
    only ever tracked their *position* while waiting, per PRD 5.1). Returns
    the number admitted. Safe to call on a schedule; an empty queue is a
    no-op."""
    key = _queue_key(event_id)
    popped = await redis.zpopmin(key, batch_size)
    if not popped:
        return 0

    now = datetime.now(UTC)
    admitted = 0
    for member, _score in popped:
        session_id = uuid.UUID(member)
        session = await db.get(BuyerSession, session_id)
        if session is None:
            continue
        session.admitted_at = now
        session.admission_token = secrets.token_urlsafe(32)
        admitted += 1

    await db.commit()
    return admitted


async def admit_all_due_events(db: AsyncSession, redis: Redis, batch_size: int) -> dict[str, int]:
    """Runs one admission tick across every event that has ever seen a
    /queue/join, i.e. what the background loop calls on each interval."""
    event_id_strs: list[str] = list(await redis.smembers(_QUEUE_EVENTS_SET))
    results: dict[str, int] = {}
    for event_id_str in event_id_strs:
        event_id = uuid.UUID(event_id_str)
        count = await admit_next_batch(db, redis, event_id, batch_size)
        if count:
            results[event_id_str] = count
        remaining = await redis.zcard(_queue_key(event_id))
        if remaining == 0:
            await redis.srem(_QUEUE_EVENTS_SET, event_id_str)
    return results


async def require_admission(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> BuyerSession:
    """Production wiring for gating hold/checkout behind the waiting room --
    NOT applied to any route by default (see app/api/routes.py). To turn the
    waiting room into a hard requirement, add
    `_: BuyerSession = Depends(require_admission)` to the hold/checkout route
    signatures. We deliberately leave that switch off here because flipping
    it would gate every buyer session through /queue/join first, which
    breaks the Phase 1 invariant suite's assumption that hold/checkout are
    directly callable, and would require every other in-flight branch
    touching these routes to also go through the queue in their tests."""
    session = await db.get(BuyerSession, session_id)
    if session is None or session.admitted_at is None:
        raise HTTPException(status_code=403, detail="not admitted from waiting room")
    return session
