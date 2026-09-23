from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SimRun(Base):
    """One deterministic simulator run (PRD section 6). `seed` is the
    reproduction key: rerunning `simulate --seed <seed>` with the same
    `params` must produce the exact same event interleaving and, if a bug is
    present, the exact same invariant violations."""

    __tablename__ = "sim_runs"

    seed: Mapped[int] = mapped_column(Integer, primary_key=True)
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    invariant_violations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
