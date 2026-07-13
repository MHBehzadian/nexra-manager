"""The campaign sending engine (central round-robin dispatcher).

A single **dispatcher** loop hands out numbers one at a time, rotating through
the active accounts, with a 1-minute gap between consecutive numbers:

    account A → number X   (spawns greeting→wait→voice task)
    …wait 1 minute…
    account B → number Y
    …wait 1 minute…
    account C → number Z
    …

Rotating accounts + the 1-minute gap means that even if the channel "Task" edit
lands late, the next number goes to a *different* account, so nobody is messaged
twice. The atomic DB claim is the hard guarantee on top of that.

Per number, a background task runs the two phases so the dispatcher never blocks
on the long greeting→voice wait:

    resolve phone → greeting → "Task ✅" → wait 15 min–2 h → voice/images → "Task ✅✅"

Account trouble (spam-limited / banned / deauthorized) is caught: the reason is
written on the channel message, the account is pulled from rotation, and the
number is handed to another account (re-greeted if it wasn't greeted yet, or
resumed at the voice step if it was).
"""

from __future__ import annotations

import asyncio
import random
from collections import deque
from datetime import datetime, timedelta, timezone

from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
from telethon.tl.types import InputPhoneContact

from accounts import manager
from database import NumberStatus
from utils import get_logger

from . import content
from .campaign_config import CampaignConfig
from .media import MediaLibrary

log = get_logger(__name__)

_ALBUM_CHUNK = 10  # Telegram allows up to 10 photos per grouped message.

# Channel "Task" markers.
MARK_GREETED = "✅"
MARK_DONE = "✅✅"
ERR_NO_ACCOUNT = "❌ این شماره اکانت تلگرام ندارد"
ERR_PRIVACY = "❌ امکان ارسال به این کاربر نیست"
ERR_ACCOUNT_LIMITED = "❌ اکانت محدود شد؛ انتقال به اکانت دیگر"
ERR_SEND = "❌ خطا در ارسال"

# Exception class-name hints for classifying failures.
# Temporary account limits → rest the account, keep it in rotation.
_ACCOUNT_COOLDOWN_HINTS = (
    "PeerFlood",
    "UserRestricted",
    "FloodWait",
    "SlowMode",
    "Takeout",
)
# Terminal account problems → remove the account permanently.
_ACCOUNT_DEAD_HINTS = (
    "UserDeactivated",
    "UserDeactivatedBan",
    "PhoneNumberBanned",
    "AuthKeyUnregistered",
    "SessionRevoked",
    "SessionExpired",
    "UserBannedInChannel",
)
# The target person (not the account) is the problem.
_TARGET_ISSUE_HINTS = (
    "UserPrivacyRestricted",
    "UserIsBlocked",
    "ChatWriteForbidden",
    "YouBlockedUser",
)


def _hint(exc: Exception, hints: tuple[str, ...]) -> bool:
    return any(h in type(exc).__name__ for h in hints)


def _classify(exc: Exception) -> str:
    """Return one of: 'target', 'cooldown', 'dead', 'other'."""
    if _hint(exc, _TARGET_ISSUE_HINTS):
        return "target"
    if _hint(exc, _ACCOUNT_DEAD_HINTS):
        return "dead"
    if _hint(exc, _ACCOUNT_COOLDOWN_HINTS):
        return "cooldown"
    return "other"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SenderEngine:
    """Central dispatcher that coordinates sending across all accounts."""

    def __init__(self, coordinator) -> None:
        self.coord = coordinator
        self.settings = coordinator.settings
        self.db = coordinator.db
        self.store = coordinator.store
        self.media = MediaLibrary()
        self.cfg = CampaignConfig()

        # Set by BotApp once the bot client is connected.
        self.bot_client = None
        self.admin_id: int | None = None

        self._clients: dict[str, object] = {}     # account phone -> TelegramClient
        self._accounts: list[dict] = []           # rotation of active accounts
        self._rot = 0
        # account phone -> UTC time its temporary limit cooldown ends (~hours)
        self._cooldowns: dict[str, datetime] = {}
        # account phone -> UTC time it is next allowed to send (normal per-account
        # rest of 40 min – 2 h between numbers, so one account isn't overused)
        self._next_ready: dict[str, datetime] = {}
        # Numbers greeted-but-not-voiced that need finishing (voice only).
        # Filled once at startup and appended to on phase-2 account failures.
        # In-memory so in-flight numbers are never resumed by another account.
        self._resume_queue: deque[str] = deque()
        self._dispatcher: asyncio.Task | None = None
        self._number_tasks: set[asyncio.Task] = set()
        self._claim_lock = asyncio.Lock()
        self._running = False
        self._exhausted_notified = False
        self.stats: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------ #
    @property
    def is_running(self) -> bool:
        return self._running and self._dispatcher is not None and not self._dispatcher.done()

    @property
    def active_workers(self) -> int:
        return len(self._accounts)

    def status(self) -> dict:
        return {
            "running": self.is_running,
            "workers": self.active_workers,
            "in_flight": sum(1 for t in self._number_tasks if not t.done()),
            "stats": {k: dict(v) for k, v in self.stats.items()},
        }

    # ------------------------------------------------------------------ #
    async def start(self) -> dict:
        if self.is_running:
            return {"started": 0, "error": "کمپین از قبل در حال اجراست."}
        if not self.coord.has_channel:
            return {"started": 0, "error": "ابتدا کانال را تنظیم کن."}
        if not self.media.is_ready():
            return {"started": 0, "error": "ابتدا «به‌روزرسانی مدیا» را بزن."}

        requeued = await self.db.requeue_incomplete()
        if requeued:
            log.info("Requeued {} not-yet-greeted number(s) on start.", requeued)

        await self._build_rotation()
        if not self._accounts:
            return {"started": 0, "error": "هیچ اکانت فعال و معتبری موجود نیست."}

        # Load numbers interrupted by a previous run (greeted, no voice yet).
        self._resume_queue = deque(await self.db.list_resumable())
        if self._resume_queue:
            log.info("Loaded {} number(s) to resume (voice only).", len(self._resume_queue))

        self._rot = 0
        self._cooldowns.clear()
        self._next_ready.clear()
        self._exhausted_notified = False
        self._running = True
        self._dispatcher = asyncio.create_task(self._dispatcher_loop(), name="dispatcher")
        log.success("Sender engine started with {} account(s).", len(self._accounts))
        return {"started": len(self._accounts), "error": None}

    async def stop(self) -> None:
        if self._dispatcher is None and not self._number_tasks:
            self._running = False
            return
        log.info("Stopping sender engine…")
        if self._dispatcher is not None:
            self._dispatcher.cancel()
        for task in list(self._number_tasks):
            task.cancel()
        pending = [t for t in [self._dispatcher, *self._number_tasks] if t is not None]
        await asyncio.gather(*pending, return_exceptions=True)
        self._number_tasks.clear()
        self._dispatcher = None
        await self._disconnect_all()
        self._running = False
        log.info("Sender engine stopped.")

    # ------------------------------------------------------------------ #
    async def _build_rotation(self) -> None:
        self._clients.clear()
        self._accounts.clear()
        self.stats.clear()
        for acc in await self.store.list():
            if acc.get("status") != "active":
                continue
            name, phone = acc.get("session_name"), acc.get("phone")
            client = manager.build_client(self.settings, name)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await self.store.update_status(name, "inactive")
                    await client.disconnect()
                    continue
            except Exception:
                log.exception("Could not connect account {}", name)
                continue
            self._clients[phone] = client
            self._accounts.append(acc)
            self.stats[phone] = {"sent": 0, "unknown": 0, "failed": 0}

    async def _disconnect_all(self) -> None:
        for client in self._clients.values():
            try:
                await client.disconnect()
            except Exception:
                pass
        self._clients.clear()
        self._accounts.clear()

    def _available_at(self, phone: str, now: datetime) -> datetime:
        """When this account may next send (max of its cooldown and rest times)."""
        ends = [t for t in (self._cooldowns.get(phone), self._next_ready.get(phone)) if t]
        future = [t for t in ends if t > now]
        return max(future) if future else now

    def _next_account(self) -> dict | None:
        """Next account in rotation that is neither cooled-down nor resting."""
        if not self._accounts:
            return None
        now = _utcnow()
        for _ in range(len(self._accounts)):
            self._rot %= len(self._accounts)
            acc = self._accounts[self._rot]
            self._rot += 1
            if self._available_at(acc.get("phone"), now) <= now:
                return acc
        return None  # everyone is resting / on cooldown right now

    def _mark_rested(self, phone: str) -> None:
        """Rest an account 40 min – 2 h after it takes a number."""
        secs = content.random_delay(content.BETWEEN_NUMBERS)
        self._next_ready[phone] = _utcnow() + timedelta(seconds=secs)

    def _soonest_available_wait(self) -> float:
        """Seconds until the earliest account becomes available (capped by caller)."""
        now = _utcnow()
        avail = [self._available_at(a.get("phone"), now) for a in self._accounts]
        future = [t for t in avail if t > now]
        if not future:
            return 1.0
        return max(1.0, (min(future) - now).total_seconds())

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._number_tasks.add(task)
        task.add_done_callback(self._number_tasks.discard)

    # ------------------------------------------------------------------ #
    # Dispatcher
    # ------------------------------------------------------------------ #
    async def _dispatcher_loop(self) -> None:
        try:
            while True:
                await self._wait_for_window()

                account = self._next_account()
                if account is None:
                    if not self._accounts:
                        await self._notify_admin("⛔️ اکانت فعالی باقی نمانده؛ کمپین متوقف شد.")
                        break
                    # Everyone is resting/cooling down → wait until one is ready.
                    wait = self._soonest_available_wait()
                    log.info("All accounts resting — sleeping {:.0f}s.", wait)
                    await asyncio.sleep(min(wait, 300))
                    continue
                acct_phone = account["phone"]

                # Prefer finishing interrupted numbers (voice only). These come
                # from an in-memory queue so a number that is still in-flight is
                # never handed to a second account.
                if self._resume_queue:
                    resume = self._resume_queue.popleft()
                    self._exhausted_notified = False
                    self._mark_rested(acct_phone)
                    self._spawn(self._resume_voice(account, resume))
                    await asyncio.sleep(content.DISPATCH_GAP_SECONDS)
                    continue

                async with self._claim_lock:
                    phone = await self.db.claim_next_pending(acct_phone)
                if phone is None:
                    # Queue empty — don't rest the account for nothing.
                    await self._maybe_notify_exhausted()
                    await asyncio.sleep(content.IDLE_POLL_SECONDS)
                    continue

                self._exhausted_notified = False
                self._mark_rested(acct_phone)
                log.info("[dispatch] {} → {}", account.get("session_name"), phone)
                self._spawn(self._process(account, phone))

                # Global 1-minute gap before the next number/account.
                await asyncio.sleep(content.DISPATCH_GAP_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Dispatcher crashed.")
        finally:
            self._running = False

    # ------------------------------------------------------------------ #
    # Per-number processing
    # ------------------------------------------------------------------ #
    async def _process(self, account: dict, phone: str) -> None:
        acct_phone = account["phone"]
        client = self._clients.get(acct_phone)
        if client is None:
            await self.db.release_pending(phone)
            return

        # Phase 1 — resolve + greeting.
        try:
            user = await self._resolve_user(client, phone)
            if user is None:
                await self.db.set_status(phone, NumberStatus.UNKNOWN)
                await self.coord.mark_channel(phone, ERR_NO_ACCOUNT)
                self._bump(acct_phone, "unknown")
                return
            await self._wait_for_window()
            await self._safe_send(client.send_message, user, content.random_greeting())
            await self.db.mark_text_sent(phone)
            await self.coord.mark_channel(phone, MARK_GREETED)
            log.info("[{}] greeting sent to {}", acct_phone, phone)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_failure(account, phone, exc, phase=1)
            return

        # Phase 2 — wait, then voice/images.
        try:
            await asyncio.sleep(content.random_delay(self.cfg.voice_delay()))
            await self._send_voice(client, user)
            await self._finish(acct_phone, phone, user, client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_failure(account, phone, exc, phase=2)

    async def _resume_voice(self, account: dict, phone: str) -> None:
        acct_phone = account["phone"]
        client = self._clients.get(acct_phone)
        if client is None:
            await self.db.assign(phone, None)
            return
        try:
            user = await self._resolve_user(client, phone)
            if user is None:
                await self.db.set_status(phone, NumberStatus.UNKNOWN)
                await self.coord.mark_channel(phone, ERR_NO_ACCOUNT)
                self._bump(acct_phone, "unknown")
                return
            await self._send_voice(client, user)
            await self._finish(acct_phone, phone, user, client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_failure(account, phone, exc, phase=2)

    async def _finish(self, acct_phone: str, phone: str, user, client) -> None:
        await self.db.mark_voice_sent(phone)
        await self.db.set_status(phone, NumberStatus.COMPLETED)
        await self.coord.mark_channel(phone, MARK_DONE)
        self._bump(acct_phone, "sent")
        log.success("[{}] completed {}", acct_phone, phone)
        await self._delete_contact(client, user)

    async def _send_voice(self, client, user) -> None:
        """Send the forwarded media items in the exact order they were added."""
        await self._wait_for_window()
        for item in self.media.load():
            kind = item["type"]
            if kind == "voice":
                await self._safe_send(client.send_file, user, item["path"], voice_note=True)
            elif kind == "image":
                await self._safe_send(client.send_file, user, item["path"])
            elif kind == "text":
                await self._safe_send(client.send_message, user, item["text"])

    # ------------------------------------------------------------------ #
    # Failure handling
    # ------------------------------------------------------------------ #
    async def _handle_failure(self, account: dict, phone: str, exc: Exception, *, phase: int) -> None:
        acct_phone = account["phone"]
        name = type(exc).__name__
        kind = _classify(exc)

        if kind == "target":
            # The *number/person* is the problem, not the account.
            log.info("Target issue for {}: {}", phone, name)
            await self.db.set_status(phone, NumberStatus.UNKNOWN)
            await self.coord.mark_channel(phone, ERR_PRIVACY)
            self._bump(acct_phone, "unknown")
            return

        if kind in ("cooldown", "dead"):
            # Account-side problem → hand this number to another account.
            await self.coord.mark_channel(phone, ERR_ACCOUNT_LIMITED)
            if phase == 1:
                await self.db.release_pending(phone)      # not greeted → re-greet elsewhere
            else:
                await self.db.assign(phone, None)         # greeted → resume voice elsewhere
                self._resume_queue.append(phone)

            if kind == "cooldown":
                # Temporary limit — rest the account, keep it in rotation.
                await self._cooldown_account(account, name)
            else:
                # Terminal — remove the account for good.
                await self._disable_account(account, name)
            return

        # Unknown/transient error — record the reason, don't retry endlessly.
        log.exception("Send failed for {} on account {}", phone, acct_phone)
        await self.db.set_status(phone, NumberStatus.UNKNOWN)
        await self.coord.mark_channel(phone, f"{ERR_SEND}: {name}")
        self._bump(acct_phone, "failed")

    async def _cooldown_account(self, account: dict, reason: str) -> None:
        """Temporarily rest a limited account (stays in rotation, reconnected)."""
        name, phone = account.get("session_name"), account.get("phone")
        hours = content.ACCOUNT_COOLDOWN_SECONDS / 3600
        self._cooldowns[phone] = _utcnow() + timedelta(seconds=content.ACCOUNT_COOLDOWN_SECONDS)
        log.warning("Account {} on cooldown for {:.1f}h ({}).", name, hours, reason)
        await self._notify_admin(
            f"😴 اکانت <b>{name}</b> موقتاً محدود شد ({reason}) و حدود "
            f"<b>{hours:.0f} ساعت</b> استراحت می‌کند، سپس خودکار به چرخه برمی‌گردد.\n"
            "شماره‌ی مربوطه به اکانت دیگری منتقل شد."
        )

    async def _disable_account(self, account: dict, reason: str) -> None:
        """Permanently remove a dead (banned/deauthorized) account."""
        name, phone = account.get("session_name"), account.get("phone")
        await self.store.update_status(name, "inactive")
        self._accounts = [a for a in self._accounts if a.get("phone") != phone]
        self._cooldowns.pop(phone, None)
        self._next_ready.pop(phone, None)
        client = self._clients.pop(phone, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        await self._notify_admin(
            f"⛔️ اکانت <b>{name}</b> بن/غیرفعال شد ({reason}) و به‌طور دائم از چرخه خارج شد.\n"
            "شماره‌ی مربوطه به اکانت دیگری منتقل شد."
        )

    # ------------------------------------------------------------------ #
    # Telegram helpers
    # ------------------------------------------------------------------ #
    async def _resolve_user(self, client, phone: str):
        result = await client(
            ImportContactsRequest(
                [
                    InputPhoneContact(
                        client_id=random.randint(0, 2**31 - 1),
                        phone=phone,
                        first_name="Contact",
                        last_name="",
                    )
                ]
            )
        )
        return result.users[0] if result.users else None

    async def _delete_contact(self, client, user) -> None:
        try:
            await client(DeleteContactsRequest(id=[user]))
        except Exception:
            log.debug("Could not delete temporary contact (non-fatal).")

    async def _safe_send(self, fn, *args, **kwargs):
        """Call a send function, retrying transparently on FloodWait."""
        for attempt in range(3):
            try:
                return await fn(*args, **kwargs)
            except FloodWaitError as exc:
                log.warning("FloodWait {}s (attempt {}/3) — sleeping.", exc.seconds, attempt + 1)
                await asyncio.sleep(exc.seconds + 5)
        return await fn(*args, **kwargs)  # last try — let errors propagate

    async def _wait_for_window(self) -> None:
        while not content.in_active_window():
            wait = content.seconds_until_window()
            log.info("Outside active window — sleeping {:.0f}s.", wait)
            await asyncio.sleep(min(wait, 300) if wait > 0 else 60)

    # ------------------------------------------------------------------ #
    def _bump(self, acct_phone: str, key: str) -> None:
        self.stats.setdefault(acct_phone, {"sent": 0, "unknown": 0, "failed": 0})
        self.stats[acct_phone][key] += 1

    async def _notify_admin(self, text: str) -> None:
        # Goes to the admin DM and the report channel (if configured).
        await self.coord.notify(text)

    async def _maybe_notify_exhausted(self) -> None:
        if self._exhausted_notified:
            return
        counts = await self.db.counts_by_status()
        if counts.get("pending", 0) == 0 and counts.get("used", 0) == 0:
            self._exhausted_notified = True
            await self._notify_admin(
                "⚠️ شماره‌های <b>pending</b> تمام شدند. "
                "برای ادامه، شماره‌های جدید را از کانال بخوان."
            )
