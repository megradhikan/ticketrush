"""
Integration test for the realtime layer (PRD 4.2): a real WS connection
receives a live snapshot on connect, then a `diff_batch` message when a seat
changes state elsewhere, via the actual Redis pub/sub fanout -- not a mock.

This runs the real FastAPI app (lifespan included, so the Redis
psubscribe listener in app.services.realtime is actually running) under a
real uvicorn server on a local port, drives the REST hold/release/checkout
endpoints against it with httpx, and drives the WS endpoint with a real
`websockets` client. `fastapi.testclient.TestClient`/`httpx.ASGITransport`
can't exercise a real WebSocket + a separately-running lifespan task talking
to real Redis the way this needs, hence the real server.
"""

import asyncio
import json
import socket
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
import uvicorn
import websockets

from app.db.session import engine as app_db_engine
from app.main import app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def live_server() -> AsyncIterator[str]:
    """Runs app.main.app under a real uvicorn server (lifespan on, so the
    Redis fanout listener task is actually running) and yields its base
    HTTP URL."""
    # app.db.session.engine is a process-wide singleton created once at
    # import time, but pytest-asyncio gives each test function a fresh
    # event loop -- and asyncpg connections can't cross loops (same reason
    # conftest.py's own `engine` fixture is function-scoped). Dispose any
    # connections pooled under a previous test's loop so the app's engine
    # opens fresh ones under *this* loop.
    await app_db_engine.dispose()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("uvicorn server did not start in time")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


async def test_ws_snapshot_and_hold_diff(live_server, venue_with_seats, make_session):
    """Connect a WS client for the event, confirm the initial snapshot
    reflects live Postgres state, then hold a seat over REST (a separate
    connection entirely) and confirm the WS client receives a diff_batch
    with that seat now `held`."""
    _, event, seats = venue_with_seats
    seat = seats[0]
    session_id = await make_session()

    ws_url = live_server.replace("http://", "ws://") + f"/events/{event.id}/stream"

    async with websockets.connect(ws_url) as ws:
        raw_snapshot = await asyncio.wait_for(ws.recv(), timeout=5)
        snapshot = json.loads(raw_snapshot)

        assert snapshot["type"] == "snapshot"
        by_id = {s["seat_id"]: s for s in snapshot["seats"]}
        assert len(by_id) == len(seats)
        assert by_id[str(seat.id)]["status"] == "available"
        assert by_id[str(seat.id)]["held_until"] is None

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_server}/events/{event.id}/seats/hold",
                json={"session_id": str(session_id), "seat_ids": [str(seat.id)]},
            )
        assert resp.status_code == 200, resp.text

        raw_diff = await asyncio.wait_for(ws.recv(), timeout=5)
        diff = json.loads(raw_diff)

        assert diff["type"] == "diff_batch"
        assert len(diff["diffs"]) == 1
        d = diff["diffs"][0]
        assert d["seat_id"] == str(seat.id)
        assert d["status"] == "held"
        assert d["held_until"] is not None


async def test_ws_checkout_diff_after_hold(live_server, venue_with_seats, make_session):
    """Same shape, but through checkout: hold then checkout (payment forced
    to succeed) should fan out a `sold` diff to the WS client."""
    _, event, seats = venue_with_seats
    seat = seats[1]
    session_id = await make_session()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{live_server}/events/{event.id}/seats/hold",
            json={"session_id": str(session_id), "seat_ids": [str(seat.id)]},
        )
        assert resp.status_code == 200, resp.text

    ws_url = live_server.replace("http://", "ws://") + f"/events/{event.id}/stream"

    async with websockets.connect(ws_url) as ws:
        raw_snapshot = await asyncio.wait_for(ws.recv(), timeout=5)
        snapshot = json.loads(raw_snapshot)
        by_id = {s["seat_id"]: s for s in snapshot["seats"]}
        assert by_id[str(seat.id)]["status"] == "held"

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{live_server}/events/{event.id}/checkout",
                json={
                    "session_id": str(session_id),
                    "seat_ids": [str(seat.id)],
                    "idempotency_key": str(uuid.uuid4()),
                },
            )
        assert resp.status_code == 200, resp.text
        order_status = resp.json()["order"]["status"]

        raw_diff = await asyncio.wait_for(ws.recv(), timeout=5)
        diff = json.loads(raw_diff)

        assert diff["type"] == "diff_batch"
        d = diff["diffs"][0]
        assert d["seat_id"] == str(seat.id)
        assert d["status"] == ("sold" if order_status == "paid" else "available")
