"""Coordinator that orchestrates work across all user accounts.

Responsibilities
----------------
* Own the (mutable, persisted) numbers-channel id.
* Make accounts join the channel (individually or all at once).
* Read the channel ascending — per account, remembering each account's last
  processed ``message_id`` (stored in the DB) — parse ``+98…`` phone numbers,
  and upsert them into the numbers table.

The coordinator is the single hub the bot handlers talk to; it ties together
``AccountStore`` (account metadata, JSON), ``Database`` (numbers + cursors), and
``manager`` (Telethon session lifecycle).
"""

from __future__ import annotations

import contextlib
import html
import re
from dataclasses import dataclass, field

from telethon import TelegramClient
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from config import Settings, persist_channel_id, persist_report_channel_id
from utils import get_logger

from . import manager
from .store import AccountStore

log = get_logger(__name__)

# Phone numbers in the channel are Iranian: +98 followed by 9–10 digits.
# Kept as a module constant so it's easy to broaden later if needed.
PHONE_IN_TEXT_RE = re.compile(r"\+98\d{9,10}")

# Marker appended to a channel message once its number enters the pipeline.
# The reader skips any message that already contains it.
TASK_MARKER = "Task"


def _channel_ref(channel_id: str) -> str | int:
    """Return an int id when the identifier is purely numeric, else the string.

    Telethon's ``get_entity`` accepts a username (str), an invite link (str) or
    a numeric peer id (int); numeric ids must be passed as ``int``.
    """
    candidate = channel_id.strip()
    if candidate.lstrip("-").isdigit():
        return int(candidate)
    return candidate


def _is_invite_link(channel_id: str) -> bool:
    c = channel_id.lower()
    return "joinchat" in c or "/+" in c or c.startswith("+")


@dataclass
class ReadResult:
    """Outcome of reading the channel with one account."""

    account: str
    ok: bool = True
    new_numbers: int = 0
    scanned: int = 0
    last_id: int = 0
    error: str | None = None


@dataclass
class JoinSummary:
    joined: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)


class AccountCoordinator:
    """Central coordinator between accounts, the channel, and the database."""

    def __init__(self, settings: Settings, store: AccountStore, db) -> None:
        self.settings = settings
        self.store = store
        self.db = db
        # Mutable at runtime; initial value comes from config/.env.
        self.channel_id: str | None = settings.channel_id
        # Optional second channel that also receives reports/backups/notices.
        self.report_channel_id: str | None = settings.report_channel_id
        # The control bot's own client — set by BotApp once connected. Used to
        # edit channel messages (members can't edit channel posts; the bot,
        # as a channel admin, can) and to DM/post reports.
        self.bot_client = None
        # Distinct channel-edit problems already reported (notify once per reason).
        self._edit_notified: set[str] = set()

    # ------------------------------------------------------------------ #
    # Channel configuration
    # ------------------------------------------------------------------ #
    def set_channel(self, value: str | None) -> str | None:
        """Persist a new channel id to .env and update the in-memory value."""
        norm = persist_channel_id(value)
        self.channel_id = norm
        log.info("Coordinator channel set to {}", norm)
        return norm

    def set_report_channel(self, value: str | None) -> str | None:
        """Persist a new report channel to .env and update the in-memory value."""
        norm = persist_report_channel_id(value)
        self.report_channel_id = norm
        log.info("Coordinator report channel set to {}", norm)
        return norm

    @property
    def has_channel(self) -> bool:
        return bool(self.channel_id)

    # ------------------------------------------------------------------ #
    # Admin / report-channel notifications
    # ------------------------------------------------------------------ #
    def _notify_targets(self) -> list:
        """Where reports/notices go.

        If a report channel is configured, everything goes there ONLY. Otherwise
        it falls back to the admin's private chat.
        """
        if self.report_channel_id:
            return [_channel_ref(self.report_channel_id)]
        return [self.settings.admin_id]

    async def notify(self, text: str) -> None:
        """Send a text notice to the admin (and the report channel, if set)."""
        if self.bot_client is None:
            return
        for target in self._notify_targets():
            try:
                await self.bot_client.send_message(target, text, parse_mode="html")
            except Exception:
                log.exception("Failed to notify target {}", target)

    async def send_report_file(self, path: str, caption: str = "") -> bool:
        """Send a file to the admin (and the report channel, if set)."""
        if self.bot_client is None:
            return False
        sent = False
        for target in self._notify_targets():
            try:
                await self.bot_client.send_file(target, path, caption=caption)
                sent = True
            except Exception:
                log.exception("Failed to send file to target {}", target)
        return sent

    # ------------------------------------------------------------------ #
    # Client helper
    # ------------------------------------------------------------------ #
    @contextlib.asynccontextmanager
    async def _account_client(self, session_name: str):
        """Yield a connected Telethon client for an account, then disconnect."""
        client = manager.build_client(self.settings, session_name)
        await client.connect()
        try:
            yield client
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()

    def account_client(self, session_name: str):
        """Public alias: context manager yielding a connected account client."""
        return self._account_client(session_name)

    async def get_channel_entity(self, client: TelegramClient):
        """Resolve the configured channel to a Telethon entity (or None)."""
        if not self.channel_id:
            return None
        return await client.get_entity(_channel_ref(self.channel_id))

    async def first_active_account(self) -> dict | None:
        """Return the first account marked active, if any."""
        for account in await self.store.list():
            if account.get("status") == "active":
                return account
        return None

    # ------------------------------------------------------------------ #
    # Joining
    # ------------------------------------------------------------------ #
    async def join_with_client(self, client: TelegramClient) -> bool:
        """Join the configured channel using an already-connected client."""
        if not self.channel_id:
            return False
        try:
            if _is_invite_link(self.channel_id):
                invite = self.channel_id.rstrip("/").split("/")[-1].lstrip("+")
                await client(ImportChatInviteRequest(invite))
            else:
                entity = await client.get_entity(_channel_ref(self.channel_id))
                await client(JoinChannelRequest(entity))
            return True
        except UserAlreadyParticipantError:
            return True  # already a member — that's a success for our purposes
        except Exception as exc:
            # Telethon raises "UserAlreadyParticipant"-style errors for invites too.
            if "already" in str(exc).lower():
                return True
            log.warning("Join failed for channel {}: {}", self.channel_id, exc)
            return False

    async def join_account(self, account: dict) -> bool:
        """Connect one account and make it join the channel."""
        name = account.get("session_name")
        async with self._account_client(name) as client:
            if not await client.is_user_authorized():
                log.warning("Account {} is not authorized; skipping join.", name)
                await self.store.update_status(name, "inactive")
                return False
            joined = await self.join_with_client(client)
            if joined:
                log.info("Account {} joined the channel.", name)
            return joined

    async def join_all(self) -> JoinSummary:
        """Make every stored account join the channel."""
        summary = JoinSummary()
        if not self.has_channel:
            return summary
        for account in await self.store.list():
            try:
                if await self.join_account(account):
                    summary.joined += 1
                else:
                    summary.failed += 1
                    summary.failures.append(account.get("session_name", "?"))
            except Exception:
                log.exception("join_account crashed for {}", account.get("session_name"))
                summary.failed += 1
                summary.failures.append(account.get("session_name", "?"))
        return summary

    # ------------------------------------------------------------------ #
    # Reading numbers (ascending, per-account cursor)
    # ------------------------------------------------------------------ #
    async def read_for_account(self, account: dict) -> ReadResult:
        """Read new channel messages for one account and store parsed numbers.

        Reads ascending (oldest→newest) starting *after* the account's stored
        ``last_message_id``, parses ``+98…`` numbers, upserts them, and advances
        the cursor. The channel's first message (a voice message, used later)
        carries no text and is simply skipped by the parser.
        """
        name = account.get("session_name")
        phone = account.get("phone")
        result = ReadResult(account=name)

        if not self.channel_id:
            result.ok = False
            result.error = "کانالی تنظیم نشده است."
            return result

        last_id = await self.db.get_cursor(phone)
        result.last_id = last_id
        batch: list[tuple[str, int, str]] = []

        try:
            async with self._account_client(name) as client:
                if not await client.is_user_authorized():
                    await self.store.update_status(name, "inactive")
                    result.ok = False
                    result.error = "اکانت غیرفعال است."
                    return result

                entity = await client.get_entity(_channel_ref(self.channel_id))
                max_id = last_id
                # reverse=True -> ascending; min_id -> only messages after cursor.
                async for message in client.iter_messages(
                    entity, reverse=True, min_id=last_id
                ):
                    result.scanned += 1
                    if message.id > max_id:
                        max_id = message.id
                    text = message.message or ""  # voice/media messages have no text
                    # Skip messages already marked as taken ("Task", any case).
                    if TASK_MARKER.lower() in text.lower():
                        continue
                    for match in PHONE_IN_TEXT_RE.findall(text):
                        batch.append((match, message.id, text[:512]))

                if batch:
                    result.new_numbers = await self.db.add_numbers(batch)
                if max_id > last_id:
                    await self.db.set_cursor(phone, self.channel_id, max_id)
                    result.last_id = max_id
        except Exception as exc:
            log.exception("read_for_account failed for {}", name)
            result.ok = False
            result.error = str(exc)

        return result

    async def read_all(self) -> list[ReadResult]:
        """Run an ascending read for every account and return per-account results."""
        results: list[ReadResult] = []
        for account in await self.store.list():
            results.append(await self.read_for_account(account))
        return results

    # ------------------------------------------------------------------ #
    # Channel message marking ("Task" + progress ticks / error reason)
    # ------------------------------------------------------------------ #
    async def _edit_diag(self, key: str, message: str) -> None:
        """DM the admin about a channel-edit problem, once per distinct reason."""
        log.warning("channel-edit issue [{}]: {}", key, message)
        if key in self._edit_notified:
            return
        self._edit_notified.add(key)
        if self.bot_client is None:
            return
        try:
            await self.bot_client.send_message(
                self.settings.admin_id,
                f"⚠️ <b>ادیت پیام کانال انجام نشد</b>\n\n{message}",
                parse_mode="html",
            )
        except Exception:
            log.exception("Failed to DM admin the edit diagnostic")

    async def test_edit(self) -> tuple[bool, str]:
        """Non-destructively test editing a real number post; return (ok, reason)."""
        if self.bot_client is None:
            return False, "کلاینت بات آماده نیست (کمپین را استارت کن)."
        if not self.channel_id:
            return False, "کانال تنظیم نشده."
        rows = await self.db.list_numbers(limit=1)
        if not rows:
            return False, "هیچ شماره‌ای در حافظه نیست؛ اول «📥 خواندن شماره‌ها» را بزن."
        row = rows[0]
        mid = row.get("source_message_id")
        original = row.get("source_text") or row.get("phone")
        if not mid:
            return False, (
                "شماره بدون «شناسه‌ی پیام کانال» ذخیره شده (نسخه‌ی قدیمی).\n"
                "«🗑 پاک‌کردن حافظه» و بعد «📥 خواندن شماره‌ها» را بزن."
            )
        try:
            entity = await self.get_channel_entity(self.bot_client)
        except Exception as exc:
            return False, f"resolve کانال ناموفق: {type(exc).__name__}"
        try:
            await self.bot_client.edit_message(entity, mid, f"{original}\n\n(تست ادیت nexra manager)")
            await self.bot_client.edit_message(entity, mid, original)  # restore
            return True, "ادیت با موفقیت انجام و به حالت اول بازگردانده شد."
        except Exception as exc:
            return False, f"{type(exc).__name__} — {exc}"

    async def mark_channel(self, phone: str, marker: str) -> bool:
        """Edit the source channel message to show this number's task status.

        Best-effort: the DB is the source of truth, so sending is unaffected if
        this fails — but every distinct failure reason is reported to the admin
        once, so the cause (permission / resolve / missing source id) is obvious.
        """
        if self.bot_client is None:
            await self._edit_diag(
                "bot_client_none",
                "کلاینت بات آماده نیست. کمپین را از منوی «🚀 کمپین» دوباره استارت کن.",
            )
            return False
        if not self.channel_id:
            await self._edit_diag("no_channel", "کانال شماره‌ها تنظیم نشده است.")
            return False

        message_id, source_text = await self.db.get_source(phone)
        if not message_id:
            await self._edit_diag(
                "no_source",
                "شماره‌ها بدون «شناسه‌ی پیام کانال» ذخیره شده‌اند (با نسخه‌ی قدیمی خوانده شده‌اند).\n"
                "راه‌حل: 📇 شماره‌ها → «🗑 پاک‌کردن حافظه‌ی شماره‌ها»، سپس «📥 خواندن شماره‌ها».",
            )
            return False

        base = (source_text or phone).split(f"\n\n{TASK_MARKER}")[0].rstrip()
        new_text = f"{base}\n\n{TASK_MARKER} {marker}".strip()

        try:
            entity = await self.get_channel_entity(self.bot_client)
        except Exception as exc:
            await self._edit_diag(
                f"resolve:{type(exc).__name__}",
                f"بات نتوانست کانال را پیدا کند (resolve): <code>{type(exc).__name__}</code>.\n"
                "یک پیام از کانال برای بات فوروارد کن (تا شناسه‌اش را یاد بگیرد)، "
                "یا کانال را با <code>@username</code> تنظیم کن.",
            )
            return False

        try:
            await self.bot_client.edit_message(entity, message_id, new_text)
            self._edit_notified.clear()  # working again → allow future re-reporting
            return True
        except Exception as exc:
            await self._edit_diag(
                f"edit:{type(exc).__name__}",
                f"ادیت ناموفق: <code>{type(exc).__name__}</code> — {html.escape(str(exc))}\n"
                "تیک «Edit Messages» را در دسترسی ادمینِ بات در کانال روشن کن.",
            )
            return False
