"""
Seeds a large venue+event in the running Postgres instance for the k6 load
test in hold_checkout_test.js, and writes out the event id + full seat id
list it needs (k6's JS runtime has no Postgres driver of its own, so it
can't query for seats itself).

Reuses app.db.seed.create_venue_with_seats -- the same factory Phase 1's
tests and the simulator use -- rather than inventing a separate seeding
path, so this venue looks exactly like any other the app already knows how
to serve.

Run from backend/, with its venv:
    backend/.venv/bin/python ../infra/loadtest/seed_event.py

Sizing: default is 4 sections x 100 rows x 50 seats = 20,000 seats,
sized comfortably above the number of hold+checkout iterations a
sustained-load run is expected to produce (see infra/loadtest/README.md),
since each seat is consumed by exactly one successful checkout -- run out
of seats and later iterations just start hitting 409s on an already-sold
seat instead of exercising a fresh hold/checkout, which would understate
throughput.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.db.seed import create_venue_with_seats  # noqa: E402
from app.db.session import AsyncSessionLocal, engine  # noqa: E402


async def main(sections: int, rows: int, seats_per_row: int, out_dir: Path) -> None:
    async with AsyncSessionLocal() as db:
        venue, event, seats = await create_venue_with_seats(
            db,
            name="Load Test Arena",
            sections=sections,
            rows_per_section=rows,
            seats_per_row=seats_per_row,
        )
    await engine.dispose()

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "event_id.txt").write_text(str(event.id))
    (out_dir / "seats.json").write_text(json.dumps([str(s.id) for s in seats]))
    print(f"Seeded event {event.id} ({venue.name}) with {len(seats)} seats -> {out_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sections", type=int, default=4)
    parser.add_argument("--rows", type=int, default=100)
    parser.add_argument("--seats-per-row", type=int, default=50)
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent / "seed_data")
    args = parser.parse_args()
    asyncio.run(main(args.sections, args.rows, args.seats_per_row, args.out_dir))
