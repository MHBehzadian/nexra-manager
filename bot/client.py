"""The central bot application wrapper.

``BotApp`` owns the Telethon client lifecycle: connect, login as a bot,
register handlers, notify the admin that it is online, and run until stopped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    AccessTokenExpiredError,
    AccessTokenInvalidError,
    ApiIdInvalidError,
)

from accounts import AccountCoordinator, AccountStore
from config import Settings
from database import Database
from sender import Reporter, SenderEngine
from utils import get_logger

from .handlers import register_handlers

log = get_logger(__name__)

# The bot's own session lives next to the project so restarts are instant.
_SESSION_DIR = Path(__file__).resolve().parent.parent / "sessions"
_SESSION_NAME = "control_bot"


class BotApp:
    """Lifecycle manager for the admin control bot."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._started_at: datetime | None = None

        # Shared services wired together through the coordinator.
        self.store = AccountStore()
        self.db = Database()
        self.coordinator = AccountCoordinator(settings, self.store, self.db)
        self.engine = SenderEngine(self.coordinator)
        self.reporter = Reporter(self.coordinator, self.engine)

        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(
            session=str(_SESSION_DIR / _SESSION_NAME),
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            # Reasonable defaults for a long-running service.
            connection_retries=5,
            retry_delay=2,
            auto_reconnect=True,
        )

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Connect, authenticate as the bot, and register handlers."""
        log.info("Initializing database…")
        await self.db.init()

        log.info("Connecting to Telegram…")
        try:
            await self.client.start(bot_token=self.settings.bot_token)
        except (AccessTokenInvalidError, AccessTokenExpiredError) as exc:
            raise RuntimeError(
                "BOT_TOKEN is invalid or expired. Re-check it with @BotFather."
            ) from exc
        except ApiIdInvalidError as exc:
            raise RuntimeError(
                "API_ID / API_HASH are invalid. Re-check them at my.telegram.org."
            ) from exc

        me = await self.client.get_me()
        self._started_at = datetime.now(timezone.utc)
        log.success("Bot online as @{} (id={}).", me.username, me.id)

        # Give the coordinator/engine the bot client so they can edit channel
        # messages ("Task" markers) and DM the admin (notices/reports).
        self.coordinator.bot_client = self.client
        self.engine.bot_client = self.client
        self.engine.admin_id = self.settings.admin_id

        register_handlers(
            self.client,
            self.coordinator,
            self.engine,
            self.reporter,
            started_at=self._started_at,
        )

        await self.reporter.start()
        await self._notify_admin_online(me.username)

    async def _notify_admin_online(self, username: str | None) -> None:
        """Best-effort DM to the admin confirming the bot is up."""
        try:
            handle = f"@{username}" if username else "(this bot)"
            await self.client.send_message(
                self.settings.admin_id,
                f"✅ <b>nexra manager</b> آنلاین شد ({handle}).\n"
                "برای باز کردن منو، /start را بفرست.",
                parse_mode="html",
            )
        except Exception:
            # Admin may not have pressed /start yet → can't DM. Not fatal.
            log.warning(
                "Could not DM the admin on startup "
                "(they may need to /start the bot first)."
            )

    async def run_until_disconnected(self) -> None:
        """Block until the client disconnects (Ctrl-C or network shutdown)."""
        log.info("Bot is now running. Press Ctrl+C to stop.")
        await self.client.run_until_disconnected()

    async def stop(self) -> None:
        """Gracefully stop the campaign, disconnect, and release resources."""
        try:
            await self.reporter.stop()
        except Exception:
            log.exception("Error stopping the reporter")
        try:
            await self.engine.stop()
        except Exception:
            log.exception("Error stopping the sender engine")
        if self.client.is_connected():
            log.info("Disconnecting…")
            await self.client.disconnect()
            log.info("Disconnected cleanly.")
        try:
            await self.db.close()
        except Exception:
            log.exception("Error closing the database")
