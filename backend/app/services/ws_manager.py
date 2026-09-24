"""
In-process registry of open WebSocket connections, keyed by event_id.

This is deliberately dumb: it knows nothing about Redis or seat state, only
"which local sockets are watching which event." app.services.realtime's
`redis_fanout_listener` is what decides a message is relevant to this
process and hands it here to actually be written to sockets. Kept as a
separate module (rather than folded into realtime.py or routes.py) so the
WS endpoint in routes.py and the fanout listener can both depend on it
without importing each other.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict

from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[uuid.UUID, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, event_id: uuid.UUID, ws: WebSocket) -> None:
        async with self._lock:
            self._connections[event_id].add(ws)

    async def disconnect(self, event_id: uuid.UUID, ws: WebSocket) -> None:
        async with self._lock:
            conns = self._connections.get(event_id)
            if conns is not None:
                conns.discard(ws)
                if not conns:
                    del self._connections[event_id]

    async def broadcast_local(self, event_id: uuid.UUID, message: str) -> None:
        """Send `message` (already-serialized JSON) to every local socket
        connected for this event. A dead socket is dropped rather than
        allowed to fail the broadcast for everyone else."""
        async with self._lock:
            conns = list(self._connections.get(event_id, ()))
        for ws in conns:
            try:
                await ws.send_text(message)
            except Exception:
                logger.debug("realtime: dropping dead WS connection for event %s", event_id, exc_info=True)
                await self.disconnect(event_id, ws)


connection_manager = ConnectionManager()
