"""
Redis pub/sub fanout for realtime seat-map updates (PRD 4.2).

This decouples "a seat's state changed somewhere in the app" from "every
WebSocket client watching that event should be told about it": whoever
mutates seat_state (holds.py, checkout.py) calls `publish_seat_diffs` with
the ORM rows it just changed; that publishes one small JSON message to a
Redis channel keyed by event_id (`event:{event_id}:seat_diffs`). Every API
process holding open WS connections for that event runs
`redis_fanout_listener` (started once in app.main's lifespan), which
psubscribes to all such channels and forwards each message to its own local
connections via `app.services.ws_manager.connection_manager`. This is the
standard way to fan out realtime updates across multiple server processes
without them needing to know about each other directly.

A Redis outage must never break an actual seat transaction: every publish
here swallows and logs its own errors rather than raising, and is awaited
in-line (not literally `asyncio.create_task`'d) so tests can assert on a
diff immediately after the REST call that triggered it returns.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.services.ws_manager import connection_manager

if TYPE_CHECKING:
    from app.models.seat_state import SeatState

logger = logging.getLogger(__name__)

settings = get_settings()

_redis: aioredis.Redis | None = None

CHANNEL_PATTERN = "event:*:seat_diffs"


def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


def _channel(event_id: uuid.UUID) -> str:
    return f"event:{event_id}:seat_diffs"


def _seat_diff(row: SeatState) -> dict[str, Any]:
    return {
        "seat_id": str(row.seat_id),
        "status": str(row.status),
        "held_until": row.held_until.isoformat() if row.held_until else None,
    }


async def _publish_batch(event_id: uuid.UUID, diffs: list[dict[str, Any]]) -> None:
    if not diffs:
        return
    try:
        r = _get_redis()
        await r.publish(_channel(event_id), json.dumps({"type": "diff_batch", "diffs": diffs}))
    except Exception:
        # Fire-and-forget: a broken/unreachable Redis must never fail the
        # seat hold/release/checkout that already committed successfully.
        logger.warning("realtime: failed to publish seat diffs for event %s", event_id, exc_info=True)


async def publish_seat_diffs(rows: Iterable[SeatState]) -> None:
    """Publish diffs for a batch of already-mutated, already-committed
    SeatState rows, one message per event_id. Rows are typically all for a
    single event (hold/release/checkout), but release_expired_holds can
    sweep across multiple events in one call, hence the grouping."""
    by_event: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_event[row.event_id].append(_seat_diff(row))
    for event_id, diffs in by_event.items():
        await _publish_batch(event_id, diffs)


async def redis_fanout_listener() -> None:
    """Long-running task (one per API process): subscribes to every event's
    diff channel and forwards each message to this process's local WS
    connections. Started in app.main's lifespan; cancelled on shutdown."""
    r = _get_redis()
    pubsub = r.pubsub()
    await pubsub.psubscribe(CHANNEL_PATTERN)
    try:
        async for message in pubsub.listen():
            if message.get("type") != "pmessage":
                continue
            channel = message["channel"]
            try:
                event_id = uuid.UUID(channel.split(":")[1])
            except (IndexError, ValueError):
                logger.warning("realtime: could not parse event_id from channel %r", channel)
                continue

            await connection_manager.broadcast_local(event_id, message["data"])
    finally:
        await pubsub.punsubscribe(CHANNEL_PATTERN)
        await pubsub.aclose()
