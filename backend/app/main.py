import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app.api.routes import router as api_router
from app.core.config import get_settings
from app.services.realtime import close_redis, redis_fanout_listener

settings = get_settings()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Single Redis pub/sub listener for this process's lifetime (PRD 4.2):
    # forwards every event's seat diffs to whichever local WS connections
    # are watching that event. See app.services.realtime.
    listener_task = asyncio.create_task(redis_fanout_listener())
    try:
        yield
    finally:
        listener_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener_task
        await close_redis()


app = FastAPI(title="TicketRush", version="0.1.0", lifespan=lifespan)
app.include_router(api_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "environment": settings.environment}
