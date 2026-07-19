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
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.channels import EditAdminRequest, JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import ChatAdminRights, InputPeerChannel
from telethon.utils import get_peer_id

from config import Settings, persist_channel_id, persist_report_channel_id
from utils import get_logger

from . import manager
from .store import AccountStore

# Persisted access to the numbers channel for the BOT (private channels can't be
# resolved by a bot from a bare id — BotMethodInvalidError — so we cache the
# access hash the bot learns from a forwarded message).
_BOT_CH_PATH = Path(__file__).resolve().parent.parent / "data" / "bot_channel.json"
# Channels the bot can reach (marked_id -> access_hash), learned from forwards.
# Lets the bot post to a private report channel it otherwise can't resolve by id.
_BOT_PEERS_PATH = Path(__file__).resolve().parent.parent / "data" / "bot_peers.json"

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
        # Cached session name of a user account that can edit channel posts.
        self._editor_session: str | None = None
        # Cached InputPeerChannel the bot can use to edit posts (see _BOT_CH_PATH).
        self._bot_channel_input: InputPeerChannel | None = None
        self._load_bot_channel()
        # marked_id(str) -> [raw_id, access_hash] for channels the bot can post to.
        self._bot_peers: dict[str, list[int]] = self._load_bot_peers()
        self._report_diag_notified = False

    # ------------------------------------------------------------------ #
    # Channel configuration
    # ------------------------------------------------------------------ #
    def set_channel(self, value: str | None) -> str | None:
        """Persist a new channel id to .env and update the in-memory value."""
        norm = persist_channel_id(value)
        self.channel_id = norm
        # A new channel invalidates the cached bot access hash.
        self._bot_channel_input = None
        with contextlib.suppress(OSError):
            _BOT_CH_PATH.unlink(missing_ok=True)
        log.info("Coordinator channel set to {}", norm)
        return norm

    # ---- bot channel access (for editing private-channel posts) --------- #
    def _load_bot_channel(self) -> None:
        try:
            if _BOT_CH_PATH.exists():
                data = json.loads(_BOT_CH_PATH.read_text(encoding="utf-8"))
                if data.get("channel_id") == self.channel_id:
                    self._bot_channel_input = InputPeerChannel(
                        int(data["id"]), int(data["access_hash"])
                    )
                    log.info("Loaded cached bot channel access.")
        except Exception:
            log.exception("Could not load cached bot channel access")

    def _configured_numeric_id(self) -> int | None:
        """The numbers channel's raw numeric id, if it was set as a numeric id."""
        cid = (self.channel_id or "").strip()
        if cid.startswith("-100") and cid[1:].isdigit():
            return int(cid[4:])  # strip the -100 bot-api prefix
        if cid.lstrip("-").isdigit():
            return int(cid.lstrip("-"))
        return None

    def save_bot_channel(self, entity) -> bool:
        """Remember the bot's access hash for the channel (from a forwarded msg)."""
        peer_id = getattr(entity, "id", None)
        access_hash = getattr(entity, "access_hash", None)
        if peer_id is None or access_hash is None:
            return False
        # Only cache access for the actual numbers channel (not e.g. the report one).
        want = self._configured_numeric_id()
        if want is not None and int(peer_id) != want:
            log.info("Forwarded channel {} isn't the numbers channel {}; not caching.", peer_id, want)
            return False
        self._bot_channel_input = InputPeerChannel(int(peer_id), int(access_hash))
        try:
            _BOT_CH_PATH.parent.mkdir(parents=True, exist_ok=True)
            _BOT_CH_PATH.write_text(
                json.dumps(
                    {"channel_id": self.channel_id, "id": int(peer_id), "access_hash": int(access_hash)}
                ),
                encoding="utf-8",
            )
            log.info("Saved bot channel access for editing.")
            return True
        except OSError:
            log.exception("Could not persist bot channel access")
            return False

    async def get_bot_channel(self):
        """Resolve the channel for the BOT — prefer the cached access hash."""
        if self._bot_channel_input is not None:
            return self._bot_channel_input
        # Fallback: works for public @username channels or if already cached in
        # the bot session; raises for private channels (BotMethodInvalidError).
        return await self.get_channel_entity(self.bot_client)

    def set_report_channel(self, value: str | None) -> str | None:
        """Persist a new report channel to .env and update the in-memory value."""
        norm = persist_report_channel_id(value)
        self.report_channel_id = norm
        self._report_diag_notified = False
        log.info("Coordinator report channel set to {}", norm)
        return norm

    @property
    def has_channel(self) -> bool:
        return bool(self.channel_id)

    # ------------------------------------------------------------------ #
    # Admin / report-channel notifications
    # ------------------------------------------------------------------ #
    # ---- bot-reachable channels (learned from forwarded messages) ------- #
    def _load_bot_peers(self) -> dict[str, list[int]]:
        try:
            if _BOT_PEERS_PATH.exists():
                return json.loads(_BOT_PEERS_PATH.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Could not load bot_peers.json")
        return {}

    def remember_bot_peer(self, chat) -> None:
        """Cache a channel's access hash (from a forwarded message) so the bot can
        post to it later even if it's private and can't be resolved by id."""
        peer_id = getattr(chat, "id", None)
        access_hash = getattr(chat, "access_hash", None)
        if peer_id is None or access_hash is None:
            return
        try:
            marked = str(get_peer_id(chat))
        except Exception:
            marked = str(peer_id)
        self._bot_peers[marked] = [int(peer_id), int(access_hash)]
        try:
            _BOT_PEERS_PATH.parent.mkdir(parents=True, exist_ok=True)
            _BOT_PEERS_PATH.write_text(json.dumps(self._bot_peers), encoding="utf-8")
        except OSError:
            log.exception("Could not persist bot_peers.json")
        self._report_diag_notified = False  # allow a fresh diagnostic if needed

    def _bot_target(self, channel_id: str | None):
        """A target the bot can send to: username/link as-is, numeric via cache."""
        if not channel_id:
            return None
        c = channel_id.strip()
        if c.startswith("@") or "t.me/" in c or c.startswith("+"):
            return _channel_ref(channel_id)
        peer = self._bot_peers.get(c)
        if peer:
            return InputPeerChannel(int(peer[0]), int(peer[1]))
        return _channel_ref(channel_id)

    def _notify_targets(self):
        """Where reports/notices go: the report channel only (if set), else admin."""
        if self.report_channel_id:
            return [self._bot_target(self.report_channel_id)]
        return [self.settings.admin_id]

    async def _report_diag(self, exc: Exception) -> None:
        """Tell the admin (directly) why the report channel send failed, once."""
        if self._report_diag_notified or self.bot_client is None:
            return
        self._report_diag_notified = True
        try:
            await self.bot_client.send_message(
                self.settings.admin_id,
                "⚠️ <b>ارسال به کانال گزارش ناموفق بود</b>\n"
                f"خطا: <code>{type(exc).__name__}</code>\n\n"
                "اگر کانال گزارش خصوصی است، یک پیام از آن کانال را برای بات "
                "<b>فوروارد</b> کن (تا شناسه‌اش را یاد بگیرد) و مطمئن شو بات آنجا ادمین است.",
                parse_mode="html",
            )
        except Exception:
            log.exception("Failed to DM report diagnostic")

    async def notify(self, text: str) -> None:
        """Send a text notice to the report channel (or admin DM)."""
        if self.bot_client is None:
            return
        for target in self._notify_targets():
            try:
                await self.bot_client.send_message(target, text, parse_mode="html")
            except Exception as exc:
                log.warning("Failed to notify target {}: {}", target, exc)
                if self.report_channel_id:
                    await self._report_diag(exc)

    async def send_report_file(self, path: str, caption: str = "") -> bool:
        """Send a file to the report channel (or admin DM)."""
        if self.bot_client is None:
            return False
        sent = False
        for target in self._notify_targets():
            try:
                await self.bot_client.send_file(target, path, caption=caption)
                sent = True
            except Exception as exc:
                log.warning("Failed to send file to target {}: {}", target, exc)
                if self.report_channel_id:
                    await self._report_diag(exc)
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

    async def promote_all(self) -> dict:
        """Promote every added account to channel admin with edit rights.

        Needs one account that is the channel owner (or an admin with 'Add
        Admins'). That account is used to promote all the others (and itself is
        already an admin). Returns {ok, fail, errors, promoter, error}.
        """
        if not self.channel_id:
            return {"ok": 0, "fail": 0, "error": "کانال تنظیم نشده."}
        accounts = [a for a in await self.store.list() if a.get("user_id")]
        if not accounts:
            return {"ok": 0, "fail": 0, "error": "اکانتی با شناسه‌ی کاربری نیست."}
        active = [a for a in accounts if a.get("status") == "active"]
        if not active:
            return {"ok": 0, "fail": 0, "error": "اکانت فعالی نیست."}

        rights = ChatAdminRights(
            post_messages=True,
            edit_messages=True,
            delete_messages=True,
            pin_messages=True,
        )
        promoter_errors: list[str] = []
        for promoter in active:
            try:
                async with self.account_client(promoter["session_name"]) as pc:
                    if not await pc.is_user_authorized():
                        continue
                    channel = await pc.get_entity(_channel_ref(self.channel_id))
                    by_id: dict[int, object] = {}
                    async for part in pc.iter_participants(channel):
                        by_id[part.id] = part

                    ok, fail, errs = 0, 0, []
                    others_ok = 0   # successful promotions of OTHER accounts
                    has_targets = False
                    rights_blocked = False
                    for target in accounts:
                        if target["session_name"] == promoter["session_name"]:
                            ok += 1  # promoter itself is already admin/owner
                            continue
                        has_targets = True
                        user = by_id.get(target["user_id"])
                        if user is None:
                            fail += 1
                            errs.append(f"{target['session_name']}: عضو کانال نیست")
                            continue
                        try:
                            await pc(EditAdminRequest(channel, user, rights, rank=""))
                            ok += 1
                            others_ok += 1
                        except Exception as exc:
                            fail += 1
                            errs.append(f"{target['session_name']}: {type(exc).__name__}")
                            name = type(exc).__name__
                            if others_ok == 0 and ("Admin" in name or "Rights" in name or "Forbidden" in name):
                                rights_blocked = True
                                break
                    # Accept this promoter only if it actually promoted someone
                    # (or there was nobody else to promote). Otherwise try next.
                    if rights_blocked or (has_targets and others_ok == 0):
                        promoter_errors.append(f"{promoter['session_name']}: دسترسی Add Admins ندارد")
                        continue
                    return {
                        "ok": ok,
                        "fail": fail,
                        "errors": errs,
                        "promoter": promoter["session_name"],
                        "error": None,
                    }
            except Exception as exc:
                promoter_errors.append(f"{promoter['session_name']}: {type(exc).__name__}")
        return {
            "ok": 0,
            "fail": len(accounts),
            "errors": promoter_errors,
            "error": "هیچ اکانتی دسترسی «Add Admins» نداشت.\n"
            "یک اکانت را دستی مالک/ادمین با تیک «Add Admins» کن، بعد دوباره بزن.",
        }

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
        batch: list[tuple[str, int, str]] = []          # new pending numbers
        greeted_batch: list[tuple[str, int, str]] = []  # one-tick → resume voice

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
                    low = text.lower()
                    matches = PHONE_IN_TEXT_RE.findall(text)
                    if not matches:
                        continue
                    has_task = "task" in low
                    if has_task and "✅✅" in text:
                        continue  # fully done (two ticks) → skip
                    if has_task and "✅" in text:
                        # One tick = greeted but not voiced → resume at voice step.
                        for match in matches:
                            greeted_batch.append((match, message.id, text[:512]))
                        continue
                    if has_task:
                        continue  # ❌ / other Task marker → skip
                    for match in matches:
                        batch.append((match, message.id, text[:512]))

                if batch:
                    result.new_numbers = await self.db.add_numbers(batch)
                if greeted_batch:
                    await self.db.add_greeted_numbers(greeted_batch)
                # Record EVERY occurrence (incl. duplicates + greeted) so all copies
                # of a number get Task-marked when it's messaged.
                if batch or greeted_batch:
                    await self.db.add_sources(batch + greeted_batch)
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

    async def _editor_sessions(self, prefer: str | None) -> list[str]:
        """Active account session names to try for editing, best candidate first."""
        active = [a["session_name"] for a in await self.store.list() if a.get("status") == "active"]
        if self._editor_session and self._editor_session in active:
            return [self._editor_session]
        ordered: list[str] = []
        if prefer and prefer in active:
            ordered.append(prefer)
        for s in active:
            if s not in ordered:
                ordered.append(s)
        return ordered

    async def _edit_post(
        self, message_id: int, new_text: str, prefer: str | None
    ) -> tuple[bool, str | None]:
        """Edit a channel post using a USER account (bots can't edit others' posts).

        Tries accounts until one succeeds (that one is a channel admin with edit
        rights), caches it, and returns (ok, last_error_name).
        """
        last: str | None = None
        for sess in await self._editor_sessions(prefer):
            try:
                async with self.account_client(sess) as client:
                    if not await client.is_user_authorized():
                        continue
                    entity = await client.get_entity(_channel_ref(self.channel_id))
                    await client.edit_message(entity, message_id, new_text)
                    self._editor_session = sess
                    return True, None
            except Exception as exc:
                last = type(exc).__name__
                # This account can't edit (not admin / no rights) → try the next.
                continue
        return False, last

    async def test_edit(self) -> tuple[bool, str]:
        """Detailed, non-destructive edit test: checks the message exists, then edits."""
        if not self.channel_id:
            return False, "کانال تنظیم نشده."
        rows = [r for r in await self.db.list_numbers(limit=10) if r.get("source_message_id")]
        if not rows:
            return False, (
                "هیچ شماره‌ای با «شناسه‌ی پیام کانال» نیست.\n"
                "«🗑 پاک‌کردن حافظه» و بعد «📥 خواندن شماره‌ها» را بزن."
            )
        sessions = await self._editor_sessions(None)
        if not sessions:
            return False, "اکانت فعالی موجود نیست."

        report: list[str] = []
        for sess in sessions:
            try:
                async with self.account_client(sess) as client:
                    if not await client.is_user_authorized():
                        report.append(f"• {sess}: اکانت غیرفعال")
                        continue
                    entity = await client.get_entity(_channel_ref(self.channel_id))
                    found_any = False
                    for row in rows:
                        mid = row["source_message_id"]
                        try:
                            msg = await client.get_messages(entity, ids=mid)
                        except Exception as exc:
                            report.append(f"• {sess}: واکشی پیام خطا داد ({type(exc).__name__})")
                            break
                        if msg is None:
                            continue  # this message gone → try the next number
                        found_any = True
                        is_fwd = getattr(msg, "fwd_from", None) is not None
                        has_media = getattr(msg, "media", None) is not None
                        props = (
                            f"فوروارد={'بله' if is_fwd else 'خیر'}، "
                            f"مدیا={'بله' if has_media else 'خیر'}"
                        )
                        cur = msg.message or (row.get("source_text") or row.get("phone"))
                        try:
                            await client.edit_message(entity, mid, cur + "\n\n(تست ادیت)")
                            await client.edit_message(entity, mid, cur)  # restore
                            self._editor_session = sess
                            preview = (msg.message or "")[:40].replace("\n", " ")
                            return True, (
                                f"با اکانت «{sess}» روی پیام id={mid} انجام شد ✅\n"
                                f"متن: «{preview}…» ({props})"
                            )
                        except Exception as exc:
                            hint = ""
                            if is_fwd:
                                hint = (
                                    "\n⛔️ این پیام «فوروارد‌شده» است و اصلاً قابل ویرایش نیست "
                                    "(نه با ادمین، نه با صاحب کانال). باید شماره‌ها بدون فوروارد "
                                    "و به‌صورت پیام معمولی در کانال باشند."
                                )
                            else:
                                hint = (
                                    "\nℹ️ پیام معمولی است؛ پس مشکل «دسترسی» است: تیک اختصاصی "
                                    "«Edit Messages» را در ادمینِ این اکانت روشن کن."
                                )
                            report.append(
                                f"• {sess}: پیام id={mid} هست ({props}) ولی ادیت نشد "
                                f"→ <code>{type(exc).__name__}</code>{hint}"
                            )
                            break
                    if not found_any:
                        report.append(
                            f"• {sess}: هیچ‌کدام از {len(rows)} پیام اخیر در کانال پیدا نشد "
                            "(شناسه‌ها اشتباه‌اند یا این اکانت عضو این کانال نیست)."
                        )
            except Exception as exc:
                report.append(f"• {sess}: خطا ({type(exc).__name__})")
        return False, "نتیجه‌ی تشخیص:\n" + "\n".join(report)

    async def mark_channel(self, phone: str, marker: str, prefer_session: str | None = None) -> bool:
        """Edit the source channel post to show this number's task status.

        Done with a USER account (bots can't edit others' posts). Best-effort: the
        DB is the source of truth, so sending is unaffected if this fails — but the
        cause is reported to the admin once.
        """
        if not self.channel_id:
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

        ok, err = await self._edit_post(message_id, new_text, prefer_session)
        if ok:
            self._edit_notified.clear()
            return True
        await self._edit_diag(
            f"edit:{err}",
            f"ادیت پیام کانال ناموفق بود (آخرین خطا: <code>{html.escape(str(err))}</code>).\n"
            "باید یکی از اکانت‌های تو <b>ادمین کانال با دسترسی Edit Messages</b> باشد "
            "(بات نمی‌تواند پیامِ دیگران را ادیت کند).",
        )
        return False
