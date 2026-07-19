"""Async database access layer (SQLAlchemy 2.0 + aiosqlite).

``Database`` owns the engine/session factory and exposes small, purpose-built
methods for the numbers pipeline and the per-account read cursors. All public
methods are safe to call from the bot's async handlers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from utils import get_logger

from .models import Base, Number, NumberSource, NumberStatus, ReadCursor

log = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "app.db"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    """Thin async repository over the numbers + cursor tables."""

    def __init__(self, url: str | None = None) -> None:
        if url is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            url = f"sqlite+aiosqlite:///{DEFAULT_DB_PATH.as_posix()}"
        self._engine = create_async_engine(url, echo=False, future=True)
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    async def init(self) -> None:
        """Create tables if they don't exist (+ lightweight column migrations)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Add columns introduced after a DB may already exist.
            for ddl in (
                "ALTER TABLE numbers ADD COLUMN source_text VARCHAR(512)",
                "ALTER TABLE numbers ADD COLUMN items_sent INTEGER DEFAULT 0",
            ):
                try:
                    await conn.exec_driver_sql(ddl)
                    log.info("Applied migration: {}", ddl)
                except Exception:
                    pass  # column already exists — expected
        log.info("Database initialized.")

    async def close(self) -> None:
        await self._engine.dispose()

    # ------------------------------------------------------------------ #
    # Numbers
    # ------------------------------------------------------------------ #
    async def add_numbers(
        self, items: Iterable[tuple[str, int | None, str | None]]
    ) -> int:
        """Insert new phone numbers, ignoring duplicates.

        ``items`` is an iterable of ``(phone, source_message_id, source_text)``.
        Returns how many rows were newly inserted.
        """
        added = 0
        async with self._session() as session:
            for phone, msg_id, source_text in items:
                stmt = (
                    sqlite_insert(Number)
                    .values(
                        phone=phone,
                        status=NumberStatus.PENDING,
                        source_message_id=msg_id,
                        source_text=source_text,
                    )
                    .on_conflict_do_nothing(index_elements=["phone"])
                )
                result = await session.execute(stmt)
                if result.rowcount and result.rowcount > 0:
                    added += 1
            await session.commit()
        if added:
            log.info("Inserted {} new number(s).", added)
        return added

    async def add_greeted_numbers(
        self, items: Iterable[tuple[str, int | None, str | None]]
    ) -> int:
        """Insert numbers already greeted in the channel (one Task tick) as 'used'
        with text_sent_at set, so they resume at the voice step. Ignores existing."""
        added = 0
        async with self._session() as session:
            for phone, msg_id, source_text in items:
                stmt = (
                    sqlite_insert(Number)
                    .values(
                        phone=phone,
                        status=NumberStatus.USED,
                        text_sent_at=_utcnow(),
                        source_message_id=msg_id,
                        source_text=source_text,
                    )
                    .on_conflict_do_nothing(index_elements=["phone"])
                )
                result = await session.execute(stmt)
                if result.rowcount and result.rowcount > 0:
                    added += 1
            await session.commit()
        if added:
            log.info("Inserted {} greeted-but-not-voiced number(s) to resume.", added)
        return added

    async def add_sources(self, items: Iterable[tuple[str, int | None, str | None]]) -> None:
        """Record every (phone, message_id) occurrence, so all duplicates of a
        number can be Task-marked. Ignores rows already present."""
        async with self._session() as session:
            for phone, msg_id, source_text in items:
                if msg_id is None:
                    continue
                stmt = (
                    sqlite_insert(NumberSource)
                    .values(phone=phone, message_id=msg_id, source_text=source_text)
                    .on_conflict_do_nothing(index_elements=["phone", "message_id"])
                )
                await session.execute(stmt)
            await session.commit()

    async def get_sources(self, phone: str) -> list[tuple[int, str | None]]:
        """All (message_id, source_text) occurrences of a phone in the channel."""
        async with self._session() as session:
            rows = await session.execute(
                select(NumberSource.message_id, NumberSource.source_text).where(
                    NumberSource.phone == phone
                )
            )
            return [(r[0], r[1]) for r in rows.all()]

    async def get_source(self, phone: str) -> tuple[int | None, str | None]:
        """Return ``(source_message_id, source_text)`` for a number."""
        async with self._session() as session:
            row = await session.execute(
                select(Number.source_message_id, Number.source_text).where(
                    Number.phone == phone
                )
            )
            found = row.first()
            return (found[0], found[1]) if found else (None, None)

    async def count_completed_since(self, since: datetime) -> int:
        """How many numbers had their voice sent since ``since`` (UTC)."""
        async with self._session() as session:
            result = await session.execute(
                select(func.count())
                .select_from(Number)
                .where(Number.voice_sent_at.is_not(None))
                .where(Number.voice_sent_at >= since)
            )
            return int(result.scalar_one())

    async def counts_by_status(self) -> dict[str, int]:
        counts = {status: 0 for status in NumberStatus.ALL}
        async with self._session() as session:
            rows = await session.execute(
                select(Number.status, func.count()).group_by(Number.status)
            )
            for status, count in rows.all():
                counts[status] = count
        return counts

    async def reset_numbers(self) -> int:
        """Wipe ALL numbers and read cursors. Returns how many numbers were removed."""
        async with self._session() as session:
            count = int(
                (await session.execute(select(func.count()).select_from(Number))).scalar_one()
            )
            await session.execute(delete(Number))
            await session.execute(delete(ReadCursor))
            await session.execute(delete(NumberSource))
            await session.commit()
        log.warning("Numbers memory reset — removed {} number(s) and all cursors.", count)
        return count

    async def total_numbers(self) -> int:
        async with self._session() as session:
            result = await session.execute(select(func.count()).select_from(Number))
            return int(result.scalar_one())

    async def list_numbers(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict]:
        async with self._session() as session:
            stmt = select(Number).order_by(Number.id)
            if status is not None:
                stmt = stmt.where(Number.status == status)
            stmt = stmt.limit(limit)
            rows = await session.execute(stmt)
            return [row.as_dict() for row in rows.scalars().all()]

    async def set_status(self, phone: str, status: str) -> bool:
        if status not in NumberStatus.ALL:
            raise ValueError(f"Invalid status: {status}")
        async with self._session() as session:
            result = await session.execute(
                update(Number).where(Number.phone == phone).values(status=status)
            )
            await session.commit()
            return bool(result.rowcount)

    async def assign(self, phone: str, account_phone: str | None) -> bool:
        async with self._session() as session:
            result = await session.execute(
                update(Number)
                .where(Number.phone == phone)
                .values(assigned_to=account_phone)
            )
            await session.commit()
            return bool(result.rowcount)

    async def mark_text_sent(self, phone: str) -> bool:
        async with self._session() as session:
            result = await session.execute(
                update(Number).where(Number.phone == phone).values(text_sent_at=_utcnow())
            )
            await session.commit()
            return bool(result.rowcount)

    async def get_items_sent(self, phone: str) -> int:
        async with self._session() as session:
            result = await session.execute(
                select(Number.items_sent).where(Number.phone == phone)
            )
            row = result.first()
            return int(row[0]) if row and row[0] is not None else 0

    async def set_items_sent(self, phone: str, count: int) -> None:
        async with self._session() as session:
            await session.execute(
                update(Number).where(Number.phone == phone).values(items_sent=count)
            )
            await session.commit()

    async def mark_voice_sent(self, phone: str) -> bool:
        async with self._session() as session:
            result = await session.execute(
                update(Number)
                .where(Number.phone == phone)
                .values(voice_sent_at=_utcnow())
            )
            await session.commit()
            return bool(result.rowcount)

    async def claim_next_pending(self, account_phone: str) -> str | None:
        """Atomically take the oldest pending number and mark it in-progress.

        Returns the claimed phone, or ``None`` if the queue is empty. The engine
        serializes calls with an ``asyncio.Lock`` so two workers never grab the
        same row (single-process deployment).
        """
        async with self._session() as session:
            row = await session.execute(
                select(Number)
                .where(Number.status == NumberStatus.PENDING)
                .order_by(Number.id)
                .limit(1)
            )
            number = row.scalar_one_or_none()
            if number is None:
                return None
            number.status = NumberStatus.USED
            number.assigned_to = account_phone
            await session.commit()
            return number.phone

    async def claim_resume(self, account_phone: str) -> str | None:
        """Atomically claim a number that was greeted but never got its voice.

        Used on restart to *finish* an interrupted number (send only the voice)
        instead of greeting the person again.
        """
        async with self._session() as session:
            row = await session.execute(
                select(Number)
                .where(Number.status == NumberStatus.USED)
                .where(Number.text_sent_at.is_not(None))
                .where(Number.voice_sent_at.is_(None))
                .order_by(Number.id)
                .limit(1)
            )
            number = row.scalar_one_or_none()
            if number is None:
                return None
            number.assigned_to = account_phone
            await session.commit()
            return number.phone

    async def list_resumable(self) -> list[str]:
        """Phones that were greeted but never got their voice (for startup resume).

        Only meaningful right after a restart — at runtime such numbers are
        in-flight and must NOT be picked up again (the engine tracks resume work
        in memory to avoid double-sending).
        """
        async with self._session() as session:
            rows = await session.execute(
                select(Number.phone)
                .where(Number.status == NumberStatus.USED)
                .where(Number.text_sent_at.is_not(None))
                .where(Number.voice_sent_at.is_(None))
                .order_by(Number.id)
            )
            return [r[0] for r in rows.all()]

    async def release_pending(self, phone: str) -> bool:
        """Return a number to the ``pending`` pool (used when an account fails
        *before* greeting, so a different account can take it)."""
        async with self._session() as session:
            result = await session.execute(
                update(Number)
                .where(Number.phone == phone)
                .where(Number.status != NumberStatus.COMPLETED)
                .values(status=NumberStatus.PENDING, assigned_to=None)
            )
            await session.commit()
            return bool(result.rowcount)

    async def requeue_incomplete(self) -> int:
        """Reset numbers that were claimed but not yet greeted back to ``pending``.

        Called on startup. Numbers that were already greeted (``text_sent_at``
        set) are left as ``used`` so ``claim_resume`` can finish them without
        re-greeting the person.
        """
        async with self._session() as session:
            result = await session.execute(
                update(Number)
                .where(Number.status == NumberStatus.USED)
                .where(Number.text_sent_at.is_(None))
                .values(status=NumberStatus.PENDING, assigned_to=None)
            )
            await session.commit()
            return int(result.rowcount or 0)

    # ------------------------------------------------------------------ #
    # Read cursors (ascending reading, one per account)
    # ------------------------------------------------------------------ #
    async def get_cursor(self, account_phone: str) -> int:
        async with self._session() as session:
            cursor = await session.get(ReadCursor, account_phone)
            return cursor.last_message_id if cursor else 0

    async def set_cursor(
        self, account_phone: str, channel_id: str, last_message_id: int
    ) -> None:
        async with self._session() as session:
            cursor = await session.get(ReadCursor, account_phone)
            if cursor is None:
                cursor = ReadCursor(
                    account_phone=account_phone,
                    channel_id=str(channel_id),
                    last_message_id=last_message_id,
                )
                session.add(cursor)
            else:
                cursor.channel_id = str(channel_id)
                cursor.last_message_id = last_message_id
            await session.commit()

    async def reset_cursor(self, account_phone: str) -> None:
        async with self._session() as session:
            cursor = await session.get(ReadCursor, account_phone)
            if cursor is not None:
                await session.delete(cursor)
                await session.commit()
