"""
Fixtures for testing against a REAL running Postgres instance (the one from
infra/docker-compose.dev.yml), not mocks or sqlite. The whole point of the
invariant suite (PRD 5.4) is to prove correctness under real row-locking
semantics (SELECT FOR UPDATE SKIP LOCKED), which an in-memory fake can't
reproduce.

`new_db_session()` gives each concurrent "request" in a test its own
connection, so concurrency tests exercise real lock contention between
separate Postgres transactions -- not just separate coroutines sharing one
connection/transaction (which wouldn't test locking at all).
"""

import uuid
from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio
from redis.asyncio import Redis, from_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.seed import create_venue_with_seats
from app.models.buyer_session import BuyerSession
from app.models.event import Event
from app.models.seat import Seat
from app.models.venue import Venue

settings = get_settings()


@pytest_asyncio.fixture
async def engine():
    # Function-scoped (not session-scoped): pytest-asyncio gives each test
    # function its own event loop, and asyncpg connections can't be reused
    # across loops -- a session-scoped engine here throws
    # "another operation is in progress" once you're on the second test.
    eng = create_async_engine(settings.database_url, pool_size=20, max_overflow=20)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def new_db_session(session_factory) -> Callable[[], AsyncIterator[AsyncSession]]:
    """Returns a factory for independent AsyncSessions (independent Postgres
    connections/transactions), for use in concurrency tests."""
    opened: list[AsyncSession] = []

    def _factory() -> AsyncSession:
        s = session_factory()
        opened.append(s)
        return s

    yield _factory

    for s in opened:
        await s.close()


@pytest_asyncio.fixture
async def db(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(engine):
    """Truncate everything before each test so tests don't see each other's
    data, while running against the real schema/constraints."""
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "TRUNCATE seat_state, order_seats, orders, seats, events, venues, sessions, sim_runs CASCADE"
        )
    yield


@pytest_asyncio.fixture
async def venue_with_seats(db) -> tuple[Venue, Event, list[Seat]]:
    return await create_venue_with_seats(db, sections=1, rows_per_section=2, seats_per_row=10)


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """Independent Redis client per test, against the real running Redis
    instance from infra/docker-compose.dev.yml -- same rationale as `engine`
    above: the Redis-first hold path's correctness rests on Lua scripts
    actually running atomically on a real server, which a fake can't
    reproduce. Flushed before and after so tests never see each other's
    keys (this Redis instance is dev-only / test-only, same as Postgres
    here -- see infra/docker-compose.dev.yml)."""
    client = from_url(settings.redis_url, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest_asyncio.fixture
async def make_session(session_factory) -> Callable:
    """Creates a persisted BuyerSession row and returns its id, using a
    throwaway connection so it's visible to every other session immediately."""

    async def _make() -> uuid.UUID:
        async with session_factory() as s:
            bs = BuyerSession(id=uuid.uuid4())
            s.add(bs)
            await s.commit()
            return bs.id

    return _make
