import uuid
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BuyerSession(Base):
    """A buyer's admitted session (post waiting-room). PRD calls this `sessions`;
    named BuyerSession here to avoid clashing with SQLAlchemy's own Session."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    admission_token: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
