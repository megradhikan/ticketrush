from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.routes import router as api_router
from app.core.config import get_settings
from app.db.redis_client import close_redis_client, get_redis_client
from app.db.session import engine
from app.workers.background import start_background_loops, stop_background_loops

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Waiting-room admission and the Redis/Postgres reconciler (PRD 5.1/5.2)
    # both run as in-process asyncio loops -- see app/workers/background.py
    # for why. They use their own session factory (not a request-scoped
    # `get_db`) since they run outside any request.
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = get_redis_client()
    tasks = start_background_loops(session_factory, redis, settings)
    try:
        yield
    finally:
        await stop_background_loops(tasks)
        await close_redis_client()


app = FastAPI(title="TicketRush", version="0.1.0", lifespan=lifespan)
app.include_router(api_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "environment": settings.environment}
