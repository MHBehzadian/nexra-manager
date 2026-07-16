"""SQLAlchemy ORM models for the numbers pipeline.

Tables
------
numbers        — every phone number parsed out of the channel.
read_cursors   — per-account "last processed message id" for ascending reads.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class NumberStatus:
    """Allowed values for ``Number.status``."""

    PENDING = "pending"
    USED = "used"
    UNKNOWN = "unknown"
    COMPLETED = "completed"

    ALL = (PENDING, USED, UNKNOWN, COMPLETED)


class Number(Base):
    __tablename__ = "numbers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    status: Mapped[str] = mapped_column(
        String(16), default=NumberStatus.PENDING, index=True
    )
    # Which account (by phone) is currently working this number, if any.
    assigned_to: Mapped[str | None] = mapped_column(String(24), nullable=True)
    text_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    voice_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # How many phase-2 items (voice/image/text) have already been delivered, so
    # a stop/restart resumes from the next item instead of re-sending them.
    items_sent: Mapped[int] = mapped_column(Integer, default=0)
    # Channel message this number was first seen in (used for the "Task" edit).
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Original text of that channel message (so we can rebuild it when editing).
    source_text: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "phone": self.phone,
            "status": self.status,
            "assigned_to": self.assigned_to,
            "text_sent_at": self.text_sent_at.isoformat() if self.text_sent_at else None,
            "voice_sent_at": self.voice_sent_at.isoformat() if self.voice_sent_at else None,
            "source_message_id": self.source_message_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class NumberSource(Base):
    """Every channel message a phone number appears in (a number can be posted
    several times). Used to mark ALL occurrences of a number with Task, so a
    duplicate isn't re-messaged after a reset/re-read."""

    __tablename__ = "number_sources"
    __table_args__ = (UniqueConstraint("phone", "message_id", name="uq_phone_msg"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(24), index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    source_text: Mapped[str | None] = mapped_column(String(512), nullable=True)


class ReadCursor(Base):
    __tablename__ = "read_cursors"

    # One cursor per account (identified by its phone).
    account_phone: Mapped[str] = mapped_column(String(24), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(128))
    last_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
