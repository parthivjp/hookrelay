from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Endpoint(Base):
    """A customer URL that wants to receive events."""
    __tablename__ = "endpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(2048))
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    secret: Mapped[str] = mapped_column(String(128))
    event_types: Mapped[list] = mapped_column(JSON, default=lambda: ["*"])
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    deliveries: Mapped[list["Delivery"]] = relationship(
        back_populates="endpoint", cascade="all, delete-orphan", passive_deletes=True
    )


class Event(Base):
    """Something that happened. One event can go to many endpoints."""
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    deliveries: Mapped[list["Delivery"]] = relationship(back_populates="event")


class Delivery(Base):
    """One event going to one endpoint. This row is the heart of the system."""
    __tablename__ = "deliveries"
    __table_args__ = (
        # An event can never be queued twice for the same endpoint.
        UniqueConstraint("event_id", "endpoint_id", name="uq_event_endpoint"),
        # The sweeper looks up "what is due now?" all the time, so index it.
        Index("ix_deliveries_status_next", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    endpoint_id: Mapped[int] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"))

    # pending -> in_progress -> success | pending (retry) | dead
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    event: Mapped[Event] = relationship(back_populates="deliveries")
    endpoint: Mapped[Endpoint] = relationship(back_populates="deliveries")
