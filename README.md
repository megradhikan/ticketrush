# TicketRush

A high-concurrency ticket onsale system: provably-correct seat holds under
real concurrency, a deterministic fault-injection simulator, a
fragmentation-minimizing seat allocator, and a natural-language seat search
agent on top.

Full spec: [docs/PRD.md](docs/PRD.md)

**Status: scaffolding in progress.** This README will be replaced in Phase 6
with real setup instructions, architecture diagram, and measured metrics
(latency, invariant-violation counts, orphan/group-satisfaction rates) from
phases 2-4. No numbers are posted here until they're measured.

## Layout

- `backend/` — FastAPI, Postgres (SQLAlchemy + Alembic), Redis
- `frontend/` — React + TypeScript seat map
- `infra/` — docker-compose, AWS deploy
- `simulator/` — deterministic fault-injection simulation harness
- `docs/` — PRD and design notes
