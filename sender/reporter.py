"""Admin reporting: daily statistics and data backups.

Runs a background loop that, once a day (at ``content.DAILY_REPORT_HOUR`` Tehran
time), DMs the admin a summary and sends a backup of the important data files.
Both actions are also available on demand from the bot.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils import get_logger

from . import content

log = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "app.db"
ACCOUNTS_PATH = BASE_DIR / "data" / "accounts.json"


class Reporter:
    """Daily report + backup sender, plus on-demand helpers."""

    def __init__(self, coordinator, engine) -> None:
        self.coord = coordinator
        self.engine = engine
        self.db = coordinator.db
        self._task: asyncio.Task | None = None

    @property
    def _bot(self):
        # The bot client is attached to the coordinator by BotApp.
        return self.coord.bot_client

    # ------------------------------------------------------------------ #
    async def send_backup(self, reason: str = "") -> bool:
        """Send copies of the DB + accounts file to the admin (and report channel)."""
        if self._bot is None:
            return False
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        sent_any = False
        with tempfile.TemporaryDirectory() as tmp:
            for src in (DB_PATH, ACCOUNTS_PATH):
                if not src.exists():
                    continue
                dst = Path(tmp) / f"{src.stem}-{stamp}{src.suffix}"
                try:
                    shutil.copy2(src, dst)  # copy so a mid-write DB stays consistent
                    if await self.coord.send_report_file(
                        str(dst), caption=f"💾 بکاپ {src.name} — {reason}".strip()
                    ):
                        sent_any = True
                except Exception:
                    log.exception("Failed to send backup of {}", src.name)
        return sent_any

    async def send_daily_report(self) -> None:
        """Compute and send a daily summary to the admin (and report channel)."""
        if self._bot is None:
            return
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        completed_24h = await self.db.count_completed_since(since)
        counts = await self.db.counts_by_status()
        eng = self.engine.status()

        lines = [
            "📈 <b>گزارش روزانه — nexra manager</b>\n",
            f"✅ پیام‌رسانی کامل در ۲۴ ساعت اخیر: <b>{completed_24h}</b> نفر",
            f"👷 وضعیت کمپین: <b>{'در حال اجرا' if eng['running'] else 'متوقف'}</b>"
            f" ({eng['workers']} اکانت)\n",
            "<b>صف شماره‌ها:</b>",
            f"⏳ pending: <b>{counts.get('pending', 0)}</b>",
            f"🔄 used: <b>{counts.get('used', 0)}</b>",
            f"✅ completed: <b>{counts.get('completed', 0)}</b>",
            f"❓ unknown: <b>{counts.get('unknown', 0)}</b>",
        ]
        if counts.get("pending", 0) == 0:
            lines.append("\n⚠️ شماره‌های pending تمام شده‌اند.")
        await self.coord.notify("\n".join(lines))

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="reporter")
        log.info("Reporter started.")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(content.seconds_until_daily_report())
                log.info("Sending scheduled daily report + backup.")
                try:
                    await self.send_daily_report()
                    await self.send_backup(reason="daily")
                except Exception:
                    log.exception("Daily report/backup failed")
                # Avoid double-firing within the same minute.
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            log.info("Reporter stopped.")
            raise
