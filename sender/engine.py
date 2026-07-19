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
import contextlib
import random
from collections import deque
from datetime import datetime, timedelta, timezone

from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
from telethon.tl.types import InputPhoneContact

from accounts import manager
from accounts.coordinator import TASK_MARKER
from database import NumberStatus
from utils import get_logger

from . import content
from .campaign_config import CampaignConfig
from .media import MediaLibrary

log = get_logger(__name__)

_ALBUM_CHUNK = 10  # Telegram allows up to 10 photos per grouped message.

# Channel "Task" markers. The account's session is written under it as @session.
def _m_greeted(name: str) -> str:
    return f"✅ سلام ارسال شد\n@{name}"


def _m_done(name: str) -> str:
    return f"✅✅ تکمیل شد\n@{name}"


def _m_limited(name: str) -> str:
    return f"⚠️ اکانت @{name} محدود شد؛ انتقال به اکانت دیگر"


def _m_no_account(name: str) -> str:
    return f"❌ این شماره اکانت تلگرام ندارد\nچک‌شده با @{name}"


ERR_NO_ACCOUNT = "❌ این شماره اکانت تلگرام ندارد"
ERR_PRIVACY = "❌ کاربر اجازه‌ی پیام از غریبه را نداده؛ دیگر تلاش نمی‌شود"
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
        # account phone -> number of in-flight tasks currently sending
        self._in_flight: dict[str, int] = {}
        # account phone -> consecutive "no Telegram account" results (limit hint)
        self._consec_no_user: dict[str, int] = {}
        # account phone -> phones it recently marked unknown (to requeue if limited)
        self._recent_unknowns: dict[str, list[str]] = {}
        # accounts removed permanently this run: [{"name", "reason"}]
        self._removed: list[dict] = []
        # Numbers greeted-but-not-voiced that need finishing (voice only).
        # Filled once at startup and appended to on phase-2 account failures.
        # In-memory so in-flight numbers are never resumed by another account.
        self._resume_queue: deque[str] = deque()
        self._dispatcher: asyncio.Task | None = None
        self._number_tasks: set[asyncio.Task] = set()
        self._claim_lock = asyncio.Lock()
        self._running = False
        self._exhausted_notified = False
        # Low-stock thresholds already alerted (reset when stock rises above them).
        self._alerted_thresholds: set[int] = set()
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
            "accounts": self.live_states(),
            "removed": list(self._removed),
            "stats": {k: dict(v) for k, v in self.stats.items()},
        }

    def live_states(self) -> list[dict]:
        """Per-account live state for the report screen (with emoji + label)."""
        now = _utcnow()
        out: list[dict] = []
        for acc in self._accounts:
            p = acc.get("phone")
            name = acc.get("session_name", p)
            cd = self._cooldowns.get(p)
            rest = self._next_ready.get(p)
            if cd is not None and cd > now:
                mins = int((cd - now).total_seconds() // 60)
                out.append({"name": name, "emoji": "😴", "label": f"محدود (≈{mins} دقیقه دیگر)"})
            elif self._in_flight.get(p, 0) > 0:
                out.append({"name": name, "emoji": "📤", "label": "در حال ارسال"})
            elif rest is not None and rest > now:
                mins = int((rest - now).total_seconds() // 60)
                out.append({"name": name, "emoji": "💤", "label": f"در استراحت (≈{mins} دقیقه دیگر)"})
            else:
                out.append({"name": name, "emoji": "⏳", "label": "در انتظار شماره"})
        return out

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
        self._in_flight.clear()
        self._consec_no_user.clear()
        self._recent_unknowns.clear()
        self._alerted_thresholds.clear()
        self._removed.clear()
        self._exhausted_notified = False
        self._running = True
        self._dispatcher = asyncio.create_task(self._dispatcher_loop(), name="dispatcher")
        log.success("Sender engine started with {} account(s).", len(self._accounts))
        return {"started": len(self._accounts), "error": None}

    async def stop(self, graceful: bool = True, grace_timeout: float = 30.0) -> None:
        """Stop the campaign.

        The dispatcher stops claiming new numbers immediately. In-flight numbers:
        with ``graceful`` we wait up to ``grace_timeout`` so a send in progress
        can finish its current item; whatever isn't done by then is cancelled.
        Either way, thanks to per-item progress (``items_sent``), the next start
        resumes each half-done number from the exact next item — never re-sending
        or skipping. (Numbers stuck in the long greeting→voice wait are cancelled
        and simply resume later.)
        """
        if self._dispatcher is None and not self._number_tasks:
            self._running = False
            return
        log.info("Stopping sender engine (graceful={})…", graceful)
        self._running = False
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._dispatcher
            self._dispatcher = None

        tasks = list(self._number_tasks)
        if tasks:
            if graceful:
                _, pending = await asyncio.wait(tasks, timeout=grace_timeout)
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            else:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self._number_tasks.clear()
        await self._disconnect_all()
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
                    await self._check_stock()
                    await asyncio.sleep(content.IDLE_POLL_SECONDS)
                    continue

                self._mark_rested(acct_phone)
                await self._check_stock()  # low-stock / exhausted alerts
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
        name = account.get("session_name", acct_phone)
        client = self._clients.get(acct_phone)
        if client is None:
            await self.db.release_pending(phone)
            return

        self._in_flight[acct_phone] = self._in_flight.get(acct_phone, 0) + 1
        try:
            # Phase 1 — resolve + greeting.
            try:
                user = await self._resolve_user(client, phone)
                if user is None:
                    await self._on_no_user(account, phone)
                    return
                await self._wait_for_window()
                await self._safe_send(client.send_message, user, content.random_greeting())
                await self.db.mark_text_sent(phone)
                # A real send happened → this account is healthy again.
                self._consec_no_user[acct_phone] = 0
                self._recent_unknowns.pop(acct_phone, None)
                await self._mark(account, phone, _m_greeted(name))
                log.info("[{}] greeting sent to {}", name, phone)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._handle_failure(account, phone, exc, phase=1)
                return

            # Phase 2 — wait, then voice/images.
            try:
                await asyncio.sleep(content.random_delay(self.cfg.voice_delay()))
                await self._send_voice(client, user, phone)
                await self._finish(account, phone, user, client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._handle_failure(account, phone, exc, phase=2)
        finally:
            self._in_flight[acct_phone] = max(0, self._in_flight.get(acct_phone, 1) - 1)

    async def _resume_voice(self, account: dict, phone: str) -> None:
        acct_phone = account["phone"]
        client = self._clients.get(acct_phone)
        if client is None:
            await self.db.assign(phone, None)
            return
        self._in_flight[acct_phone] = self._in_flight.get(acct_phone, 0) + 1
        try:
            user = await self._resolve_user(client, phone)
            if user is None:
                await self._on_no_user(account, phone)
                return
            await self._send_voice(client, user, phone)
            await self._finish(account, phone, user, client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_failure(account, phone, exc, phase=2)
        finally:
            self._in_flight[acct_phone] = max(0, self._in_flight.get(acct_phone, 1) - 1)

    async def _on_no_user(self, account: dict, phone: str) -> None:
        """Handle a number with no Telegram account.

        No message was sent, so the account does NOT take its 40 min–2 h rest —
        it moves on quickly. But if an account returns 'no account' for many
        numbers in a row, it is probably contact-import-limited itself: cool it
        down and requeue those numbers for other accounts.
        """
        acct_phone = account["phone"]
        # Undo the rest the dispatcher set — nothing was actually sent.
        self._next_ready.pop(acct_phone, None)
        await self.db.set_status(phone, NumberStatus.UNKNOWN)
        await self._mark(account, phone, _m_no_account(account.get("session_name", acct_phone)))
        self._bump(acct_phone, "unknown")

        self._consec_no_user[acct_phone] = self._consec_no_user.get(acct_phone, 0) + 1
        self._recent_unknowns.setdefault(acct_phone, []).append(phone)
        if self._consec_no_user[acct_phone] >= content.NO_USER_LIMIT_THRESHOLD:
            # Likely limited, not that all these numbers lack Telegram → requeue
            # them for other accounts and rest this one.
            recent = self._recent_unknowns.pop(acct_phone, [])
            self._consec_no_user[acct_phone] = 0
            for p in recent:
                await self.db.release_pending(p)
            log.warning(
                "Account {} got {} 'no account' in a row → likely limited.",
                account.get("session_name"),
                len(recent),
            )
            await self._cooldown_account(
                account, f"احتمال محدودیت (import) — {len(recent)} شماره پشت‌سرهم بی‌نتیجه"
            )

    async def _finish(self, account: dict, phone: str, user, client) -> None:
        acct_phone = account["phone"]
        name = account.get("session_name", acct_phone)
        await self.db.mark_voice_sent(phone)
        await self.db.set_status(phone, NumberStatus.COMPLETED)
        await self._mark(account, phone, _m_done(name))
        self._bump(acct_phone, "sent")
        log.success("[{}] completed {}", name, phone)
        await self._delete_contact(client, user)

    async def _mark(self, account: dict, phone: str, marker: str) -> None:
        """Edit EVERY channel post of this number with its Task status, using the
        SAME account that messaged it. A number posted several times gets all its
        copies marked, so a duplicate isn't re-messaged after a reset/re-read."""
        if not self.coord.channel_id:
            return
        sources = await self.db.get_sources(phone)
        if not sources:
            mid, txt = await self.db.get_source(phone)  # fallback (old data)
            if mid:
                sources = [(mid, txt)]
        if not sources:
            await self.coord._edit_diag(
                "no_source",
                "شماره‌ها بدون «شناسه‌ی پیام کانال» ذخیره شده‌اند.\n"
                "یک‌بار «🗑 پاک‌کردن حافظه» و بعد «📥 خواندن شماره‌ها».",
            )
            return
        client = self._clients.get(account.get("phone"))
        if client is None:
            return
        try:
            entity = await self.coord.get_channel_entity(client)
        except Exception as exc:
            await self._mark_error(account, exc)
            return
        last_exc = None
        for message_id, source_text in sources:
            base = (source_text or phone).split(f"\n\n{TASK_MARKER}")[0].rstrip()
            new_text = f"{base}\n\n{TASK_MARKER} {marker}".strip()
            try:
                await client.edit_message(entity, message_id, new_text)
            except Exception as exc:
                last_exc = exc
        if last_exc is None:
            self.coord._edit_notified.clear()
        else:
            await self._mark_error(account, last_exc)

    async def _mark_error(self, account: dict, exc: Exception) -> None:
        name = account.get("session_name")
        await self.coord._edit_diag(
            f"edit:{name}:{type(exc).__name__}",
            f"اکانت «{name}» نتوانست پیام کانال را ادیت کند → "
            f"<code>{type(exc).__name__}</code>.\n"
            "این اکانت باید ادمین کانال با «Edit Messages» باشد و پیام‌های شماره "
            "«فوروارد‌شده» نباشند.",
        )

    async def _send_voice(self, client, user, phone: str) -> None:
        """Send the forwarded media items in order, with a random 30 s–2 min gap
        between each one. Resumes from the last delivered item (items_sent), so a
        stop/restart never re-sends or skips an item."""
        items = self.media.load()
        start = await self.db.get_items_sent(phone)
        for i in range(start, len(items)):
            if i > start:
                await asyncio.sleep(content.random_delay(self.cfg.item_delay()))
            await self._wait_for_window()
            item = items[i]
            kind = item["type"]
            if kind == "voice":
                await self._safe_send(client.send_file, user, item["path"], voice_note=True)
            elif kind == "image":
                await self._safe_send(client.send_file, user, item["path"])
            elif kind == "text":
                await self._safe_send(client.send_message, user, item["text"])
            # Persist progress only AFTER the item is actually delivered.
            await self.db.set_items_sent(phone, i + 1)

    # ------------------------------------------------------------------ #
    # Failure handling
    # ------------------------------------------------------------------ #
    async def _handle_failure(self, account: dict, phone: str, exc: Exception, *, phase: int) -> None:
        acct_phone = account["phone"]
        acct_name = account.get("session_name", acct_phone)
        err = type(exc).__name__
        kind = _classify(exc)

        if kind == "target":
            # The *person* restricts who can message them → never retry (any acct).
            log.info("Target issue for {}: {}", phone, err)
            await self.db.set_status(phone, NumberStatus.UNKNOWN)
            await self._mark(account, phone, ERR_PRIVACY)
            self._bump(acct_phone, "unknown")
            return

        if kind in ("cooldown", "dead"):
            # Account-side problem → hand this number to another account.
            await self._mark(account, phone, _m_limited(acct_name))
            if phase == 1:
                await self.db.release_pending(phone)      # not greeted → re-greet elsewhere
            else:
                await self.db.assign(phone, None)         # greeted → resume voice elsewhere
                self._resume_queue.append(phone)

            if kind == "cooldown":
                # Temporary limit — rest the account, keep it in rotation.
                await self._cooldown_account(account, err)
            else:
                # Terminal — remove the account for good.
                await self._disable_account(account, err)
            return

        # Unknown/transient error — record the reason, don't retry endlessly.
        log.exception("Send failed for {} on account {}", phone, acct_name)
        await self.db.set_status(phone, NumberStatus.UNKNOWN)
        await self._mark(account, phone, f"{ERR_SEND} (@{acct_name}): {err}")
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
        self._removed.append({"name": name, "reason": reason})
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

    async def _alert(self, text: str) -> None:
        """Send an operational alert straight to the admin's private chat."""
        if self.bot_client is None or self.admin_id is None:
            return
        try:
            await self.bot_client.send_message(self.admin_id, text, parse_mode="html")
        except Exception:
            log.exception("Failed to send alert to admin")

    async def _check_stock(self) -> None:
        """Alert the admin when pending numbers drop below thresholds / run out."""
        pending = (await self.db.counts_by_status()).get("pending", 0)
        if pending == 0:
            if not self._exhausted_notified:
                self._exhausted_notified = True
                await self._alert(
                    "🔚 <b>شماره‌های صف تمام شدند!</b>\n"
                    "برای ادامه، شماره‌ی جدید در کانال بگذار و «📥 خواندن شماره‌ها» را بزن."
                )
            return
        self._exhausted_notified = False
        for th in content.LOW_STOCK_THRESHOLDS:
            if pending <= th and th not in self._alerted_thresholds:
                self._alerted_thresholds.add(th)
                await self._alert(f"⚠️ فقط <b>{pending}</b> شماره باقی مانده (زیر {th}).")
            elif pending > th:
                self._alerted_thresholds.discard(th)
