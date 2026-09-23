"""Redis-first hold/release/reconcile (PRD 5.2, strategy 2). Drop-in
alternate to app.services.holds -- same function signatures plus a redis
client, same exceptions, same all-or-nothing semantics -- but the fast path
is a Lua script against Redis instead of a Postgres row-lock scan.

Design (documented here since the PRD leaves several choices open):

  Fast path (Redis, atomic via Lua):
    For a hold on N seats, HOLD_SCRIPT checks all N `hold:{event}:{seat}`
    keys are absent and, only if every one is, SETs all N with the session
    id as value and a TTL, and adds each seat to the `held_index:{event}`
    set (a cheap membership index so the reconciler doesn't have to SCAN the
    whole keyspace). This is genuinely atomic -- Lua scripts run to
    completion on Redis's single command thread -- so it gives the same
    all-or-nothing guarantee holds.py gets from one Postgres transaction,
    without ever touching Postgres for the (common, under contention) losing
    attempts. That's the actual latency/throughput win this strategy is
    benchmarked for: under contention for one seat, only the single Redis
    winner ever issues a Postgres query; everyone else is rejected in
    sub-millisecond Redis time.

  Postgres confirmation ("async-confirmed in Postgres" per PRD 5.2):
    We deliberately do NOT fire-and-forget this into a background task.
    Instead, the winner's request awaits holds_service.hold_seats (the same
    Postgres-only function, unmodified) immediately after the Lua script
    succeeds. Two reasons:
      1. Correctness: checkout.py (untouched, Postgres-only) requires
         seat_state.status == 'held' before it will sell a seat. If
         confirmation were truly fire-and-forget, a buyer could call
         checkout in the gap before Postgres catches up and get a spurious
         409 -- a race the PRD's invariant suite would rightly catch. Since
         seat_state is the durable source of truth (PRD 4.2), checkout must
         never have to guess whether an in-flight Redis hold "really" landed.
      2. It's still fast: by the time we reach this step, Redis has already
         resolved the contention, so this Postgres call almost never
         contends with another writer for the same seat -- it's a
         near-uncontended SELECT ... FOR UPDATE SKIP LOCKED touching only
         this buyer's rows, not N buyers racing for the same row. The
         latency win over the Postgres-only strategy comes from eliminating
         *that* contention, not from skipping Postgres entirely.
    If Postgres confirmation fails (HoldError) -- which should only happen
    if Redis and Postgres have drifted out of agreement (see `reconcile`
    below) -- we roll back the Redis keys we just set and raise HoldError,
    so a confirmation failure never leaves an orphaned Redis-only hold.

  Reconciliation (PRD 5.2's explicit warning against trusting Redis TTL
  alone):
    `reconcile()` is the periodic sweep (run via a background asyncio loop,
    see app.workers.background) that repairs three kinds of drift:
      a) Redis TTL fired (or Redis lost the key outright -- process
         restart, eviction, a flushed instance) but Postgres still shows
         `held` past its own held_until: handled by re-running
         holds_service.release_expired_holds, the exact same reconciler
         Phase 1 exposes for the Postgres-only path. Postgres's own
         held_until is authoritative here, independent of whether the Redis
         key still exists.
      b) A Redis hold key exists (not yet TTL-expired) but Postgres shows
         `available` for that seat: this is the confirmation-crashed case
         (process died between the Lua script succeeding and the Postgres
         write landing). Postgres is the source of truth (PRD 4.2), so we
         release the orphaned Redis key rather than trust it -- an
         unconfirmed Redis hold blocking real buyers would itself be a
         leaked hold (invariant 3), just one Postgres never agreed to.
      c) `held_index:{event}` contains a seat whose underlying `hold:*` key
         has already expired naturally: just a stale index entry, dropped
         with no other side effects.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.state_machine import SeatStatus
from app.models.seat_state import SeatState
from app.services import holds as holds_service
from app.services.holds import HoldError, ReleaseError
from app.services.holds import release_expired_holds as pg_release_expired_holds
from app.services.holds import release_seats as pg_release_seats

# Re-exported so callers (routes.py, tests) can catch one set of exceptions
# regardless of which strategy they're using.
__all__ = ["HoldError", "ReleaseError", "hold_seats", "release_seats", "release_expired_holds", "reconcile"]


_HOLD_SCRIPT = """
local n = #KEYS
for i = 1, n do
  if redis.call('EXISTS', KEYS[i]) == 1 then
    return 0
  end
end
for i = 1, n do
  redis.call('SET', KEYS[i], ARGV[1], 'EX', ARGV[2])
  redis.call('SADD', ARGV[3], ARGV[3 + i])
end
return 1
"""

_RELEASE_SCRIPT = """
local released = {}
for i = 1, #KEYS do
  local val = redis.call('GET', KEYS[i])
  if val == ARGV[1] then
    redis.call('DEL', KEYS[i])
    redis.call('SREM', ARGV[2], ARGV[2 + i])
    table.insert(released, ARGV[2 + i])
  end
end
return released
"""


def _hold_key(event_id: uuid.UUID, seat_id: uuid.UUID) -> str:
    return f"hold:{event_id}:{seat_id}"


def _index_key(event_id: uuid.UUID) -> str:
    return f"held_index:{event_id}"


async def hold_seats(
    db: AsyncSession,
    redis: Redis,
    event_id: uuid.UUID,
    session_id: uuid.UUID,
    seat_ids: list[uuid.UUID],
    ttl_seconds: int,
) -> tuple[list[uuid.UUID], datetime]:
    if not seat_ids:
        return [], datetime.now(UTC)

    keys = [_hold_key(event_id, sid) for sid in seat_ids]
    index_key = _index_key(event_id)
    argv = [str(session_id), str(ttl_seconds), index_key, *[str(sid) for sid in seat_ids]]

    won = await redis.eval(_HOLD_SCRIPT, len(keys), *keys, *argv)
    if not won:
        raise HoldError(list(seat_ids))

    try:
        held, expires_at = await holds_service.hold_seats(db, event_id, session_id, seat_ids, ttl_seconds)
    except HoldError:
        # Redis and Postgres disagreed (see module docstring, case b) --
        # someone else legitimately holds/sold this seat in Postgres even
        # though Redis just told us we won. Undo the Redis reservation so we
        # don't leave a phantom hold behind, then surface the failure.
        release_argv = [str(session_id), index_key, *[str(sid) for sid in seat_ids]]
        await redis.eval(_RELEASE_SCRIPT, len(keys), *keys, *release_argv)
        raise

    return held, expires_at


async def release_seats(
    db: AsyncSession,
    redis: Redis,
    event_id: uuid.UUID,
    session_id: uuid.UUID,
    seat_ids: list[uuid.UUID],
) -> list[uuid.UUID]:
    if not seat_ids:
        return []

    keys = [_hold_key(event_id, sid) for sid in seat_ids]
    index_key = _index_key(event_id)
    argv = [str(session_id), index_key, *[str(sid) for sid in seat_ids]]

    # Best-effort in Redis first (fast, and safe to do even if the Postgres
    # release below fails/raises -- an extra Redis key deletion is never
    # incorrect, only a missing one is).
    await redis.eval(_RELEASE_SCRIPT, len(keys), *keys, *argv)

    return await pg_release_seats(db, event_id, session_id, seat_ids)


async def release_expired_holds(db: AsyncSession, redis: Redis, event_id: uuid.UUID | None = None) -> int:
    """Eager counterpart to Redis TTL expiry, same contract as
    holds.release_expired_holds: sweep seats whose held_until has passed
    and return them to `available` in Postgres. Also best-effort cleans up
    any Redis keys/index entries left behind for the seats it swept, since
    those are now stale by definition."""
    now = datetime.now(UTC)
    stmt = select(SeatState.seat_id, SeatState.event_id).where(
        SeatState.status == SeatStatus.HELD, SeatState.held_until < now
    )
    if event_id is not None:
        stmt = stmt.where(SeatState.event_id == event_id)
    about_to_sweep = list((await db.execute(stmt)).all())

    swept = await pg_release_expired_holds(db, event_id)

    for seat_id, ev_id in about_to_sweep:
        await redis.delete(_hold_key(ev_id, seat_id))
        await redis.srem(_index_key(ev_id), str(seat_id))

    return swept


@dataclass
class ReconcileReport:
    expired_released: int
    orphaned_redis_released: int
    stale_index_cleaned: int


async def reconcile(db: AsyncSession, redis: Redis, event_id: uuid.UUID) -> ReconcileReport:
    """The reconciler PRD 5.2 asks for: repairs drift between Redis's
    coordination state and Postgres's durable state. See the module
    docstring for the three cases this handles. Safe to call repeatedly/
    concurrently/on a schedule -- every write it makes is idempotent."""
    expired_released = await release_expired_holds(db, redis, event_id)

    orphaned_redis_released = 0
    stale_index_cleaned = 0

    index_key = _index_key(event_id)
    seat_id_strs: list[str] = list(await redis.smembers(index_key))

    for seat_id_str in seat_id_strs:
        seat_id = uuid.UUID(seat_id_str)
        key = _hold_key(event_id, seat_id)
        held_session = await redis.get(key)

        if held_session is None:
            # Case (c): Redis TTL already expired this key naturally; the
            # index entry just hasn't been cleaned up yet.
            await redis.srem(index_key, seat_id_str)
            stale_index_cleaned += 1
            continue

        row = await db.get(SeatState, {"seat_id": seat_id, "event_id": event_id})
        postgres_agrees = (
            row is not None
            and row.status == SeatStatus.HELD
            and str(row.held_by_session) == held_session
        )
        if not postgres_agrees:
            # Case (b): Redis thinks this seat is held (and not yet
            # TTL-expired) but Postgres -- the source of truth -- disagrees,
            # either because confirmation never landed or because it landed
            # for a different session than Redis's key holds. Trust
            # Postgres: release the orphaned Redis reservation so it can't
            # block a legitimate buyer forever (that would itself be a
            # leaked hold, invariant 3).
            await redis.delete(key)
            await redis.srem(index_key, seat_id_str)
            orphaned_redis_released += 1

    return ReconcileReport(
        expired_released=expired_released,
        orphaned_redis_released=orphaned_redis_released,
        stale_index_cleaned=stale_index_cleaned,
    )
