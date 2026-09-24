"""Shared async Redis client factory (Phase 2, PRD 4.2/5.2). Mirrors the
shape of app.db.session's engine/get_db pattern so Redis is wired the same
way Postgres is: one process-wide client, a FastAPI dependency to hand it to
routes, and nothing route-specific baked in here.
"""

from collections.abc import AsyncGenerator

from redis.asyncio import Redis, from_url

from app.core.config import get_settings

settings = get_settings()

_redis_client: Redis | None = None


def get_redis_client() -> Redis:
    """Process-wide singleton client (connection-pooled internally by
    redis-py), created lazily so importing this module never opens a socket
    -- useful for the Postgres-only test suite and the postgres hold
    strategy, neither of which need Redis at all."""
    global _redis_client
    if _redis_client is None:
        _redis_client = from_url(settings.redis_url, decode_responses=True)
    return _redis_client


async def get_redis() -> AsyncGenerator[Redis, None]:
    yield get_redis_client()


async def close_redis_client() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
