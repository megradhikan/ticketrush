"""Background asyncio loops started from the FastAPI app lifespan
(app/main.py): the Redis/Postgres hold reconciler (PRD 5.2) and the waiting
room's batch admission ticker (PRD 5.1).

Choice of mechanism: a plain `asyncio.create_task` loop inside the same
process, not a separate worker process/cron job or a task queue (Celery/RQ/
arq). For a single-instance portfolio deployment this is simpler to run and
demo (no extra process to deploy, no broker), and both loops are cheap,
idempotent, and safe to run concurrently with themselves (SKIP LOCKED on the
Postgres side, plain overwrites on the Redis side) -- exactly the properties
you'd want before trusting a naive in-process scheduler. The documented
tradeoff: this doesn't horizontally scale past one process (every replica
would run its own loop, duplicating work -- harmless here since both loops
are idempotent, but wasteful) and a process crash pauses reconciliation
until the process restarts. A production deployment would move these to a
proper scheduler (e.g. a Celery beat task, or a k8s CronJob hitting a
`/internal/reconcile` endpoint) -- noted here rather than built, since the
PRD scoped this as "your call, but document the choice."
"""

import asyncio
import contextlib
import logging
import uuid

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.event import Event
from app.services import queue as queue_service
from app.services import redis_holds

logger = logging.getLogger("ticketrush.background")


async def _all_event_ids(session_factory: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    from sqlalchemy import select

    async with session_factory() as db:
        return list((await db.execute(select(Event.id))).scalars())


async def reconcile_loop(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
) -> None:
    """Sweeps Redis/Postgres drift for every event on a fixed interval.
    Runs regardless of `hold_strategy` -- harmless (no-op) for events that
    never took a Redis-first hold, and means flipping the strategy setting
    doesn't also require restarting the reconciler."""
    interval = max(settings.waiting_room_batch_interval_seconds, 1.0)
    while True:
        try:
            event_ids = await _all_event_ids(session_factory)
            for event_id in event_ids:
                async with session_factory() as db:
                    report = await redis_holds.reconcile(db, redis, event_id)
                    if report.orphaned_redis_released or report.expired_released:
                        logger.info(
                            "reconcile event=%s expired_released=%d orphaned_redis_released=%d "
                            "stale_index_cleaned=%d",
                            event_id,
                            report.expired_released,
                            report.orphaned_redis_released,
                            report.stale_index_cleaned,
                        )
        except Exception:  # noqa: BLE001 -- a bad sweep must never crash the loop
            logger.exception("reconcile_loop iteration failed")
        await asyncio.sleep(interval)


async def admission_loop(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
) -> None:
    """Admits the next waiting-room batch for every event with a nonempty
    queue, on a fixed interval (PRD 5.1)."""
    interval = max(settings.waiting_room_batch_interval_seconds, 0.1)
    while True:
        try:
            async with session_factory() as db:
                await queue_service.admit_all_due_events(db, redis, settings.waiting_room_batch_size)
        except Exception:  # noqa: BLE001
            logger.exception("admission_loop iteration failed")
        await asyncio.sleep(interval)


def start_background_loops(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
) -> list[asyncio.Task]:
    return [
        asyncio.create_task(reconcile_loop(session_factory, redis, settings), name="reconcile_loop"),
        asyncio.create_task(admission_loop(session_factory, redis, settings), name="admission_loop"),
    ]


async def stop_background_loops(tasks: list[asyncio.Task]) -> None:
    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await t
