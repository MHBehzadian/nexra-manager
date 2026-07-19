"""Admin/report-channel reporting: periodic stats, backups, on-stop reports.

Schedules (Tehran time):
  * every 6 hours (00/06/12/18)  → a report + a data backup,
  * every midnight (00:00)        → that day's stats,
  * on every campaign stop        → a full per-session report.

All reports go through ``coordinator.notify`` / ``send_report_file`` (the report
channel if one is set, else the admin DM). Also available on demand from the bot.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from utils import get_logger

from . import content

log = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "app.db"
ACCOUNTS_PATH = BASE_DIR / "data" / "accounts.json"


class Reporter:
    def __init__(self, coordinator, engine) -> None:
        self.coord = coordinator
        self.engine = engine
        self.db = coordinator.db
        self._tasks: list[asyncio.Task] = []

    @property
    def _bot(self):
        return self.coord.bot_client

    # ------------------------------------------------------------------ #
    async def send_backup(self, reason: str = "") -> bool:
        """Send copies of the DB + accounts file (report channel or admin DM)."""
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
                    shutil.copy2(src, dst)
                    if await self.coord.send_report_file(
                        str(dst), caption=f"💾 بکاپ {src.name} — {reason}".strip()
                    ):
                        sent_any = True
                except Exception:
                    log.exception("Failed to send backup of {}", src.name)
        return sent_any

    async def send_report(
        self, title: str, since_dt: datetime | None = None, status: dict | None = None
    ) -> None:
        """Build and send a report (optionally with completed-since + a status snapshot)."""
        if self._bot is None:
            return
        counts = await self.db.counts_by_status()
        eng = status if status is not None else self.engine.status()
        name_by_phone = {a.get("phone"): a.get("session_name") for a in await self.coord.store.list()}

        lines = [f"📈 <b>{title} — nexra manager</b>\n"]
        if since_dt is not None:
            done = await self.db.count_completed_since(since_dt)
            lines.append(f"✅ تکمیل‌شده در این بازه: <b>{done}</b> نفر")
        lines.append(
            f"👷 کمپین: <b>{'در حال اجرا' if eng.get('running') else 'متوقف'}</b> "
            f"({eng.get('workers', 0)} اکانت)\n"
        )
        lines.append("<b>صف شماره‌ها:</b>")
        lines.append(
            f"⏳ pending: <b>{counts.get('pending', 0)}</b> | "
            f"🔄 used: <b>{counts.get('used', 0)}</b> | "
            f"✅ completed: <b>{counts.get('completed', 0)}</b> | "
            f"❓ unknown: <b>{counts.get('unknown', 0)}</b>"
        )
        if eng.get("stats"):
            lines.append("\n<b>هر سشن (این اجرا):</b>")
            for ph, s in eng["stats"].items():
                label = name_by_phone.get(ph) or ph
                lines.append(
                    f"• @{label}: ✅ {s['sent']} | ❓ {s['unknown']} | ⚠️ {s['failed']}"
                )
        if eng.get("accounts"):
            lines.append("\n<b>وضعیت لحظه‌ای:</b>")
            for a in eng["accounts"]:
                lines.append(f"{a['emoji']} @{a['name']} — {a['label']}")
        if eng.get("removed"):
            lines.append("\n⛔️ <b>خارج‌شده:</b>")
            for r in eng["removed"]:
                lines.append(f"• @{r['name']} ({r['reason']})")
        await self.coord.notify("\n".join(lines))

    async def send_daily_report(self) -> None:
        """On-demand report (last 24h) — used by the '📈 گزارش الان' button."""
        await self.send_report("گزارش (۲۴ ساعت اخیر)", since_dt=_hours_ago(24))

    async def send_daily_stats(self) -> None:
        """Midnight: that day's stats (since 00:00 Tehran)."""
        await self.send_report("آمار امروز", since_dt=content.tehran_day_start_utc())

    async def send_stop_report(self, status: dict) -> None:
        """Full per-session report captured at campaign stop."""
        await self.send_report(
            "گزارش توقف کمپین", since_dt=content.tehran_day_start_utc(), status=status
        )

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._loop_6h(), name="reporter-6h"),
            asyncio.create_task(self._loop_midnight(), name="reporter-midnight"),
        ]
        log.info("Reporter started (6-hourly report+backup, midnight stats).")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []

    async def _loop_6h(self) -> None:
        try:
            while True:
                await asyncio.sleep(content.seconds_until_next_6h())
                log.info("6-hourly report + backup.")
                try:
                    await self.send_report("گزارش ۶ساعته", since_dt=_hours_ago(6))
                    await self.send_backup(reason="۶ساعته")
                except Exception:
                    log.exception("6-hourly report/backup failed")
                await asyncio.sleep(60)  # avoid double-fire in the same minute
        except asyncio.CancelledError:
            raise

    async def _loop_midnight(self) -> None:
        try:
            while True:
                await asyncio.sleep(content.seconds_until_midnight())
                log.info("Midnight daily stats.")
                try:
                    await self.send_daily_stats()
                except Exception:
                    log.exception("Midnight stats failed")
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise


def _hours_ago(hours: int) -> datetime:
    from datetime import timedelta

    return datetime.now(timezone.utc) - timedelta(hours=hours)
