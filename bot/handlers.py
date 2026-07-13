"""Event handlers for the admin control bot.

Every handler is admin-gated via the ``admin_only`` guard — the single most
important security boundary of the tool.

Features implemented here:
  * Main menu + status / settings / help screens.
  * Account management (add via multi-step conversation, list, remove).
  * Numbers & channel management:
      - set the numbers channel (CHANNEL_ID) from the bot,
      - make all accounts join it,
      - read numbers ascending from the channel (per-account cursor),
      - show number statistics.
  * Automatic channel-join right after an account is added.

Cross-account work is delegated to ``AccountCoordinator``; the bot handlers stay
thin and only deal with Telegram I/O and conversation state.
"""

from __future__ import annotations

import functools
import html
import re
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from accounts import AccountCoordinator, manager
from utils import get_logger

from . import keyboards
from .state import (
    AddAccountConversation,
    AddStep,
    CollectMediaConversation,
    SetChannelConversation,
    SetReportChannelConversation,
    SetVoiceDelayConversation,
    StateManager,
)

log = get_logger(__name__)

# Validation
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
_PHONE_RE = re.compile(r"^\+\d{7,15}$")
_CANCEL_WORDS = {"/cancel", "cancel", "لغو", "انصراف"}


# --------------------------------------------------------------------------- #
# Admin guard
# --------------------------------------------------------------------------- #
def _make_admin_guard(admin_id: int):
    """Build a decorator that only lets ``admin_id`` through."""

    def admin_only(handler):
        @functools.wraps(handler)
        async def wrapper(event):
            # The control bot only talks in private chats with the admin. Ignore
            # anything else — e.g. posts in the numbers channel, which the bot
            # receives because it is a channel admin. Without this, it would spam
            # "you're not allowed" replies into the channel.
            if isinstance(event, events.NewMessage.Event) and not event.is_private:
                return
            sender_id = event.sender_id
            if sender_id != admin_id:
                log.warning(
                    "Unauthorized access attempt from user_id={} ({})",
                    sender_id,
                    getattr(event, "text", "<callback>"),
                )
                try:
                    if isinstance(event, events.CallbackQuery.Event):
                        await event.answer("⛔️ دسترسی ندارید.", alert=True)
                    else:
                        await event.respond("⛔️ شما اجازه‌ی استفاده از این بات را ندارید.")
                except Exception:  # never let a rejection reply crash the loop
                    log.exception("Failed to send rejection to {}", sender_id)
                return
            try:
                await handler(event)
            except events.StopPropagation:
                # Control-flow, not an error — let Telethon's dispatcher see it.
                raise
            except Exception:
                log.exception("Handler {} failed", handler.__name__)
                try:
                    await event.respond("⚠️ خطای داخلی رخ داد. جزئیات در لاگ ثبت شد.")
                except Exception:
                    pass

        return wrapper

    return admin_only


# --------------------------------------------------------------------------- #
# Static / templated screens
# --------------------------------------------------------------------------- #
def _welcome_text(admin_name: str) -> str:
    return (
        f"👋 سلام <b>{html.escape(admin_name)}</b>!\n\n"
        "به پنل <b>nexra manager</b> خوش آمدی.\n"
        "از منوی زیر یک گزینه انتخاب کن:"
    )


def _status_text(
    started_at: datetime, account_count: int, channel: str | None, numbers_total: int
) -> str:
    uptime = datetime.now(timezone.utc) - started_at
    total_seconds = int(uptime.total_seconds())
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    channel_line = html.escape(channel) if channel else "(تنظیم‌نشده)"
    return (
        "📊 <b>وضعیت سیستم</b>\n\n"
        "• 🟢 بات: <b>آنلاین</b>\n"
        f"• ⏱ آپ‌تایم: <b>{hours}h {minutes}m {seconds}s</b>\n"
        f"• 👤 اکانت‌های ثبت‌شده: <b>{account_count}</b>\n"
        f"• 📡 کانال شماره‌ها: <code>{channel_line}</code>\n"
        f"• 🔢 شماره‌های ذخیره‌شده: <b>{numbers_total}</b>"
    )


_HELP_TEXT = (
    "❓ <b>راهنما — nexra manager</b>\n\n"
    "این پنل، مرکز کنترل ابزار اتوماسیون است.\n\n"
    "<b>دستورات:</b>\n"
    "• /start — نمایش منوی اصلی\n"
    "• /menu — همان منوی اصلی\n"
    "• /id — نمایش شناسه‌ی عددی شما\n"
    "• /cancel — لغو عملیات در حال انجام\n\n"
    "<b>بخش‌ها:</b>\n"
    "• 👤 اکانت‌ها — افزودن/لیست/حذف اکانت\n"
    "• 📇 شماره‌ها — تنظیم کانال، عضویت اکانت‌ها، خواندن و آمار شماره‌ها\n"
    "• 🚀 کمپین — به‌روزرسانی مدیا، شروع/توقف ارسال، و وضعیت"
)


def _settings_text(channel: str | None) -> str:
    channel_line = html.escape(channel) if channel else "(تنظیم‌نشده)"
    return (
        "⚙️ <b>تنظیمات</b>\n\n"
        f"📡 کانال شماره‌ها: <code>{channel_line}</code>\n\n"
        "اعتبارنامه‌ها (API/TOKEN) از فایل <code>.env</code> بارگذاری می‌شوند.\n"
        "کانال را می‌توانی از بخش «📇 شماره‌ها» تغییر دهی."
    )


def _accounts_menu_text(count: int) -> str:
    return (
        "👤 <b>مدیریت اکانت‌ها</b>\n\n"
        f"تعداد اکانت‌های ثبت‌شده: <b>{count}</b>\n"
        "یک گزینه را انتخاب کن:"
    )


def _account_detail_text(acc: dict, status: str) -> str:
    emoji = "🟢" if status == "active" else "🔴"
    status_fa = "فعال" if status == "active" else "غیرفعال"
    added = str(acc.get("added_at", "-")).replace("T", " ")
    handle = acc.get("username")
    handle_line = f"\n🔗 یوزرنیم: @{html.escape(handle)}" if handle else ""
    name = html.escape(str(acc.get("session_name", "?")))
    return (
        f"👤 <b>{name}</b>\n\n"
        f"📱 شماره: <code>{html.escape(str(acc.get('phone', '-')))}</code>\n"
        f"📶 وضعیت: {emoji} <b>{status_fa}</b>"
        f"{handle_line}\n"
        f"🕒 افزوده‌شده: <code>{html.escape(added)}</code>"
    )


# --------------------------------------------------------------------------- #
# Small input helpers
# --------------------------------------------------------------------------- #
def _normalize_phone(text: str) -> str:
    cleaned = re.sub(r"[^\d+]", "", text)
    if cleaned and not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def register_handlers(
    client: TelegramClient,
    coordinator: AccountCoordinator,
    engine,
    reporter,
    *,
    started_at: datetime,
) -> None:
    """Attach all event handlers to ``client``. Called once at startup."""

    settings = coordinator.settings
    store = coordinator.store
    db = coordinator.db
    admin_only = _make_admin_guard(settings.admin_id)
    state = StateManager()

    # ---- shared render helpers ------------------------------------------- #
    async def _send_main_menu(event, *, edit: bool = False) -> None:
        me = await event.client.get_me()
        name = me.first_name or "admin"
        text = _welcome_text(name)
        if edit:
            await event.edit(text, buttons=keyboards.main_menu(), parse_mode="html")
        else:
            await event.respond(text, buttons=keyboards.main_menu(), parse_mode="html")

    async def _show_accounts_menu(event, *, edit: bool = True) -> None:
        text = _accounts_menu_text(await store.count())
        buttons = keyboards.accounts_menu()
        if edit:
            await event.edit(text, buttons=buttons, parse_mode="html")
        else:
            await event.respond(text, buttons=buttons, parse_mode="html")

    async def _show_accounts_list(event) -> None:
        accounts = await store.list()
        if not accounts:
            await event.edit(
                "📋 هنوز هیچ اکانتی ثبت نشده است.\nبرای شروع «➕ افزودن اکانت» را بزن.",
                buttons=keyboards.accounts_menu(),
                parse_mode="html",
            )
            return
        await event.edit(
            f"📋 <b>لیست اکانت‌ها</b> ({len(accounts)})\n\nبرای جزئیات، روی هر اکانت بزن:",
            buttons=keyboards.accounts_list(accounts),
            parse_mode="html",
        )

    async def _show_numbers_menu(event, *, edit: bool = True) -> None:
        channel = coordinator.channel_id or "(تنظیم‌نشده)"
        total = await db.total_numbers()
        text = (
            "📇 <b>شماره‌ها و کانال</b>\n\n"
            f"📡 کانال: <code>{html.escape(channel)}</code>\n"
            f"🔢 کل شماره‌ها: <b>{total}</b>\n\n"
            "یک گزینه را انتخاب کن:"
        )
        buttons = keyboards.numbers_menu(coordinator.has_channel)
        if edit:
            await event.edit(text, buttons=buttons, parse_mode="html")
        else:
            await event.respond(text, buttons=buttons, parse_mode="html")

    async def _show_campaign_menu(event, *, edit: bool = True) -> None:
        running = engine.is_running
        mc = engine.media.counts()
        if mc["voices"] or mc["images"] or mc["texts"]:
            media_line = (
                f"🎙 مدیا: <b>{mc['voices']}</b> ویس، <b>{mc['images']}</b> تصویر، "
                f"<b>{mc['texts']}</b> متن (به‌ترتیب)"
            )
        else:
            media_line = "🎙 مدیا: <b>آماده نیست</b> (دکمه‌ی «تنظیم مدیا» را بزن و فوروارد کن)"
        state_line = "🟢 در حال اجرا" if running else "⛔️ متوقف"
        report_ch = coordinator.report_channel_id or "(تنظیم‌نشده)"
        text = (
            "🚀 <b>کمپین ارسال</b>\n\n"
            f"وضعیت: <b>{state_line}</b>\n"
            f"{media_line}\n"
            f"📡 کانال شماره‌ها: <code>{html.escape(coordinator.channel_id or '(تنظیم‌نشده)')}</code>\n"
            f"📮 کانال گزارش: <code>{html.escape(report_ch)}</code>\n\n"
            "یک گزینه را انتخاب کن:"
        )
        buttons = keyboards.campaign_menu(running)
        if edit:
            await event.edit(text, buttons=buttons, parse_mode="html")
        else:
            await event.respond(text, buttons=buttons, parse_mode="html")

    # ---- conversation cleanup ------------------------------------------- #
    async def _discard_conversation(
        conv: AddAccountConversation, *, remove_session: bool
    ) -> None:
        """Disconnect the temp login client and optionally delete its session."""
        if getattr(conv, "client", None) is not None:
            try:
                if conv.client.is_connected():
                    await conv.client.disconnect()
            except Exception:
                log.exception("Error disconnecting temp login client")
        if remove_session and getattr(conv, "session_name", None):
            manager.remove_session_file(conv.session_name)

    async def _abort_add(event, conv: AddAccountConversation, message: str) -> None:
        await _discard_conversation(conv, remove_session=True)
        state.clear(event.sender_id)
        await event.respond(f"❌ {message}", buttons=keyboards.accounts_menu())

    # ---- add-account steps ---------------------------------------------- #
    async def _step_name(event, conv: AddAccountConversation, text: str) -> None:
        name = text.strip()
        if not _NAME_RE.match(name):
            await event.respond(
                "⚠️ نام سشن نامعتبر است.\nفقط حروف انگلیسی، عدد، _ و - (۲ تا ۳۲ کاراکتر).\n"
                "دوباره بفرست:",
                buttons=keyboards.cancel_only(),
            )
            return
        if await store.exists(name) or manager.session_exists(name):
            await event.respond(
                "⚠️ این نام قبلاً استفاده شده است. یک نام دیگر بفرست:",
                buttons=keyboards.cancel_only(),
            )
            return
        conv.session_name = name
        conv.step = AddStep.PHONE
        await event.respond(
            f"✅ نام سشن: <b>{html.escape(name)}</b>\n\n"
            "📱 حالا شماره‌ی تلفن را همراه کد کشور بفرست (مثال: <code>+989123456789</code>):",
            buttons=keyboards.cancel_only(),
            parse_mode="html",
        )

    async def _step_phone(event, conv: AddAccountConversation, text: str) -> None:
        phone = _normalize_phone(text)
        if not _PHONE_RE.match(phone):
            await event.respond(
                "⚠️ شماره نامعتبر است. با کد کشور و به شکل <code>+98...</code> بفرست:",
                buttons=keyboards.cancel_only(),
                parse_mode="html",
            )
            return
        if await store.exists_phone(phone):
            await event.respond(
                "⚠️ این شماره قبلاً ثبت شده است. شماره‌ی دیگری بفرست یا لغو کن:",
                buttons=keyboards.cancel_only(),
            )
            return

        await event.respond("⏳ در حال ارسال کد ورود…")
        conv.client = manager.build_client(settings, conv.session_name)
        try:
            conv.phone_code_hash = await manager.send_login_code(conv.client, phone)
        except PhoneNumberInvalidError:
            await event.respond(
                "⚠️ تلگرام این شماره را نامعتبر دانست. دوباره بفرست:",
                buttons=keyboards.cancel_only(),
            )
            await _discard_conversation(conv, remove_session=True)
            conv.client = None
            return
        except FloodWaitError as exc:
            await _abort_add(
                event, conv, f"محدودیت تلگرام (FloodWait). {exc.seconds} ثانیه بعد دوباره تلاش کن."
            )
            return
        except Exception as exc:
            log.exception("send_code_request failed")
            await _abort_add(event, conv, f"ارسال کد ناموفق بود: {exc}")
            return

        conv.phone = phone
        conv.step = AddStep.CODE
        await event.respond(
            "📨 کد ورود برای این شماره ارسال شد.\n\n"
            "🔐 <b>مهم:</b> برای اینکه تلگرام کد را باطل نکند، آن را با فاصله یا خط تیره بفرست، "
            "مثلاً <code>1-2-3-4-5</code> یا <code>1 2 3 4 5</code>:",
            buttons=keyboards.cancel_only(),
            parse_mode="html",
        )

    async def _step_code(event, conv: AddAccountConversation, text: str) -> None:
        code = re.sub(r"\D", "", text)
        if not code:
            await event.respond(
                "⚠️ کد را فقط به‌صورت رقم بفرست (مثلاً <code>1 2 3 4 5</code>):",
                buttons=keyboards.cancel_only(),
                parse_mode="html",
            )
            return

        # Best-effort: hide the login code from the chat history.
        try:
            await event.delete()
        except Exception:
            pass

        try:
            await manager.sign_in_with_code(
                conv.client, conv.phone, code, conv.phone_code_hash
            )
        except SessionPasswordNeededError:
            conv.step = AddStep.PASSWORD
            await event.respond(
                "🔒 این اکانت رمز عبور دومرحله‌ای (Two-Step) دارد.\nرمز را بفرست:",
                buttons=keyboards.cancel_only(),
            )
            return
        except PhoneCodeInvalidError:
            await event.respond(
                "⚠️ کد اشتباه است. دوباره بفرست:", buttons=keyboards.cancel_only()
            )
            return
        except PhoneCodeExpiredError:
            await _abort_add(event, conv, "کد منقضی شد. لطفاً افزودن اکانت را از نو شروع کن.")
            return
        except FloodWaitError as exc:
            await _abort_add(event, conv, f"محدودیت تلگرام. {exc.seconds} ثانیه صبر کن.")
            return
        except Exception as exc:
            log.exception("sign_in with code failed")
            await _abort_add(event, conv, f"ورود ناموفق بود: {exc}")
            return

        await _finalize_add(event, conv)

    async def _step_password(event, conv: AddAccountConversation, text: str) -> None:
        password = text  # do not strip — passwords may contain edge whitespace

        # Best-effort: remove the password message from history.
        try:
            await event.delete()
        except Exception:
            pass

        try:
            await manager.sign_in_with_password(conv.client, password)
        except PasswordHashInvalidError:
            await event.respond(
                "⚠️ رمز اشتباه است. دوباره بفرست:", buttons=keyboards.cancel_only()
            )
            return
        except FloodWaitError as exc:
            await _abort_add(event, conv, f"محدودیت تلگرام. {exc.seconds} ثانیه صبر کن.")
            return
        except Exception as exc:
            log.exception("sign_in with password failed")
            await _abort_add(event, conv, f"ورود ناموفق بود: {exc}")
            return

        await _finalize_add(event, conv)

    async def _finalize_add(event, conv: AddAccountConversation) -> None:
        """Persist the account, auto-join the channel, and clean up."""
        try:
            me = await conv.client.get_me()
        except Exception:
            me = None

        # Auto-join the numbers channel while the client is still connected.
        joined = False
        if coordinator.has_channel:
            try:
                joined = await coordinator.join_with_client(conv.client)
            except Exception:
                log.exception("Auto-join failed for {}", conv.session_name)

        # Disconnecting flushes the authorized session to disk.
        try:
            await conv.client.disconnect()
        except Exception:
            log.exception("Error disconnecting after successful login")

        account = {
            "session_name": conv.session_name,
            "phone": conv.phone,
            "status": "active",
            "user_id": getattr(me, "id", None),
            "username": getattr(me, "username", None),
            "first_name": getattr(me, "first_name", None),
            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        await store.add(account)
        state.clear(event.sender_id)

        if not coordinator.has_channel:
            join_line = "ℹ️ کانالی تنظیم نشده؛ عضویت خودکار انجام نشد."
        elif joined:
            join_line = "🔗 اکانت به کانال شماره‌ها اضافه شد."
        else:
            join_line = "⚠️ عضویت خودکار در کانال ناموفق بود (بعداً دوباره تلاش کن)."

        display_name = html.escape(getattr(me, "first_name", None) or conv.session_name)
        await event.respond(
            f"✅ اکانت <b>{html.escape(conv.session_name)}</b> با موفقیت اضافه شد!\n\n"
            f"👤 نام: <b>{display_name}</b>\n"
            f"📱 شماره: <code>{html.escape(conv.phone)}</code>\n"
            f"{join_line}\n"
            f"💾 سشن در <code>sessions/{html.escape(conv.session_name)}.session</code> ذخیره شد.",
            buttons=keyboards.accounts_menu(),
            parse_mode="html",
        )
        log.success("Account '{}' added successfully (joined={}).", conv.session_name, joined)

    # ---- set-channel step ------------------------------------------------ #
    async def _step_set_channel(event, text: str) -> None:
        value = text.strip()
        if not value:
            await event.respond(
                "⚠️ مقدار خالی است. یک @username، id عددی یا لینک بفرست:",
                buttons=keyboards.cancel_only(),
            )
            return
        norm = coordinator.set_channel(value)
        state.clear(event.sender_id)
        await event.respond(
            f"✅ کانال شماره‌ها تنظیم شد:\n<code>{html.escape(norm or '')}</code>\n\n"
            "حالا می‌توانی «🔗 عضویت همه‌ی اکانت‌ها» را بزنی تا اکانت‌ها عضو شوند.",
            buttons=keyboards.numbers_menu(coordinator.has_channel),
            parse_mode="html",
        )
        log.info("Channel set via bot to {}", norm)

    # ---- set voice-delay step ------------------------------------------- #
    async def _step_set_voice_delay(event, text: str) -> None:
        parts = re.split(r"[\s,\-–]+", text.strip())
        nums = [p for p in parts if p]
        if len(nums) != 2 or not all(p.isdigit() for p in nums):
            await event.respond(
                "⚠️ فرمت اشتباه است. دو عدد (دقیقه) بفرست: «حداقل حداکثر»\n"
                "مثال: <code>15 120</code>",
                buttons=keyboards.cancel_campaign(),
                parse_mode="html",
            )
            return
        low_min, high_min = int(nums[0]), int(nums[1])
        if low_min <= 0 or high_min < low_min:
            await event.respond(
                "⚠️ باید حداقل بزرگ‌تر از صفر و حداکثر ≥ حداقل باشد. دوباره بفرست:",
                buttons=keyboards.cancel_campaign(),
            )
            return
        engine.cfg.set_voice_delay(low_min * 60, high_min * 60)
        state.clear(event.sender_id)
        await event.respond(
            f"✅ زمان ارسال ویس تنظیم شد: بین <b>{low_min}</b> تا <b>{high_min}</b> دقیقه "
            "پس از ارسال سلام.",
            buttons=keyboards.campaign_menu(engine.is_running),
            parse_mode="html",
        )

    # ---- set report-channel step ---------------------------------------- #
    async def _step_set_report_channel(event, text: str) -> None:
        value = text.strip()
        if value in {"-", "off", "حذف", "خالی"}:
            coordinator.set_report_channel(None)
            state.clear(event.sender_id)
            await event.respond(
                "✅ کانال گزارش حذف شد. گزارش‌ها فقط به‌صورت پیام خصوصی به ادمین می‌روند.",
                buttons=keyboards.campaign_menu(engine.is_running),
            )
            return
        if not value:
            await event.respond(
                "⚠️ مقدار خالی است. یک @username، id عددی یا لینک بفرست "
                "(یا «-» برای حذف):",
                buttons=keyboards.cancel_campaign(),
            )
            return
        norm = coordinator.set_report_channel(value)
        state.clear(event.sender_id)
        await event.respond(
            f"✅ کانال گزارش تنظیم شد:\n<code>{html.escape(norm or '')}</code>\n\n"
            "از این پس گزارش‌ها و بکاپ‌ها فقط به این کانال ارسال می‌شوند.\n"
            "<i>بات باید در این کانال ادمین باشد.</i>",
            buttons=keyboards.campaign_menu(engine.is_running),
            parse_mode="html",
        )

    # ---- collect-media step (admin forwards voice/images/text) ---------- #
    async def _step_collect_media(event, conv: CollectMediaConversation) -> None:
        text = (event.raw_text or "").strip().lower()
        if text in _CANCEL_WORDS:
            state.clear(event.sender_id)
            await event.respond(
                "✅ لغو شد. مدیای قبلی دست‌نخورده ماند.",
                buttons=keyboards.campaign_menu(engine.is_running),
            )
            return
        if text in {"تمام", "done", "/done", "پایان", "اتمام"}:
            result = engine.media.commit_collection()
            state.clear(event.sender_id)
            await event.respond(
                f"✅ مدیا ذخیره شد (به‌ترتیب): <b>{result['voices']}</b> ویس، "
                f"<b>{result['images']}</b> تصویر، <b>{result['texts']}</b> متن.",
                buttons=keyboards.campaign_menu(engine.is_running),
                parse_mode="html",
            )
            return

        # A voice/photo → save the file; otherwise plain text → save as a text item.
        if event.message.media is not None:
            kind = await engine.media.add_from_message(event.message)
            if kind is None:
                await event.respond(
                    "⚠️ این نوع رسانه پشتیبانی نمی‌شود. فقط ویس یا تصویر بفرست.",
                    buttons=keyboards.media_collect(),
                )
                return
            label = "ویس" if kind == "voice" else "تصویر"
        elif event.raw_text and event.raw_text.strip():
            engine.media.add_text(event.raw_text.strip())
            label = "متن"
        else:
            await event.respond(
                "⚠️ ویس، تصویر یا متن بفرست (یا «✅ پایان و ذخیره»).",
                buttons=keyboards.media_collect(),
            )
            return

        c = engine.media.collected
        await event.respond(
            f"✅ اضافه شد ({label}).\n"
            f"تا الان به‌ترتیب: <b>{c['voices']}</b> ویس، <b>{c['images']}</b> تصویر، "
            f"<b>{c['texts']}</b> متن.\nبقیه را بفرست، یا «✅ پایان و ذخیره».",
            buttons=keyboards.media_collect(),
            parse_mode="html",
        )

    # =====================================================================  #
    #  Handlers (registration order matters — conversation router first)     #
    # =====================================================================  #

    # --- Conversation input router (runs before command handlers) --------- #
    @client.on(events.NewMessage)
    @admin_only
    async def _conversation_router(event):
        conv = state.get(event.sender_id)
        if conv is None:
            return  # not in a conversation → let other handlers run

        # Media collection consumes forwarded media (no text) and a few keywords.
        if isinstance(conv, CollectMediaConversation):
            await _step_collect_media(event, conv)
            raise events.StopPropagation

        text = (event.raw_text or "").strip()

        if text.lower() in _CANCEL_WORDS:
            cleared = state.clear(event.sender_id)
            if isinstance(cleared, AddAccountConversation):
                await _discard_conversation(cleared, remove_session=True)
                await event.respond("✅ عملیات لغو شد.", buttons=keyboards.accounts_menu())
            elif isinstance(
                cleared,
                (SetVoiceDelayConversation, SetReportChannelConversation),
            ):
                await event.respond(
                    "✅ عملیات لغو شد.", buttons=keyboards.campaign_menu(engine.is_running)
                )
            else:
                await event.respond(
                    "✅ عملیات لغو شد.",
                    buttons=keyboards.numbers_menu(coordinator.has_channel),
                )
            raise events.StopPropagation

        if text.startswith("/"):
            await event.respond(
                "در حال یک عملیات هستی. برای لغو /cancel را بفرست.",
                buttons=keyboards.cancel_only(),
            )
            raise events.StopPropagation

        try:
            if isinstance(conv, SetChannelConversation):
                await _step_set_channel(event, text)
            elif isinstance(conv, SetVoiceDelayConversation):
                await _step_set_voice_delay(event, text)
            elif isinstance(conv, SetReportChannelConversation):
                await _step_set_report_channel(event, text)
            elif conv.step is AddStep.NAME:
                await _step_name(event, conv, text)
            elif conv.step is AddStep.PHONE:
                await _step_phone(event, conv, text)
            elif conv.step is AddStep.CODE:
                await _step_code(event, conv, text)
            elif conv.step is AddStep.PASSWORD:
                await _step_password(event, conv, text)
        except events.StopPropagation:
            raise
        except Exception:
            log.exception("Conversation step crashed")
            if isinstance(conv, AddAccountConversation):
                await _abort_add(event, conv, "خطای غیرمنتظره رخ داد. عملیات لغو شد.")
            else:
                state.clear(event.sender_id)
                await event.respond(
                    "❌ خطای غیرمنتظره رخ داد. عملیات لغو شد.",
                    buttons=keyboards.numbers_menu(coordinator.has_channel),
                )

        raise events.StopPropagation

    # --- /start and /menu ------------------------------------------------- #
    @client.on(events.NewMessage(pattern=r"^/(start|menu)(?:@\w+)?$"))
    @admin_only
    async def _on_start(event):
        log.info("Admin opened the main menu.")
        await _send_main_menu(event)
        raise events.StopPropagation

    # --- /id -------------------------------------------------------------- #
    @client.on(events.NewMessage(pattern=r"^/id(?:@\w+)?$"))
    @admin_only
    async def _on_id(event):
        await event.respond(
            f"🆔 شناسه‌ی عددی شما: <code>{event.sender_id}</code>", parse_mode="html"
        )
        raise events.StopPropagation

    # --- Inline button callbacks ------------------------------------------ #
    @client.on(events.CallbackQuery)
    @admin_only
    async def _on_callback(event):
        data = event.data or b""
        data_str = data.decode("utf-8", "ignore")
        log.debug("Callback: {}", data_str)

        # -- main menu --
        if data == keyboards.CB_HOME:
            await event.answer()
            await _send_main_menu(event, edit=True)

        elif data == keyboards.CB_STATUS:
            await event.answer()
            await event.edit(
                _status_text(
                    started_at,
                    await store.count(),
                    coordinator.channel_id,
                    await db.total_numbers(),
                ),
                buttons=keyboards.back_button(),
                parse_mode="html",
            )

        elif data == keyboards.CB_SETTINGS:
            await event.answer()
            await event.edit(
                _settings_text(coordinator.channel_id),
                buttons=keyboards.back_button(),
                parse_mode="html",
            )

        elif data == keyboards.CB_HELP:
            await event.answer()
            await event.edit(_HELP_TEXT, buttons=keyboards.back_button(), parse_mode="html")

        # -- accounts submenu --
        elif data == keyboards.CB_ACCOUNTS:
            await event.answer()
            await _show_accounts_menu(event)

        elif data == keyboards.CB_ACC_LIST:
            await event.answer()
            await _show_accounts_list(event)

        elif data == keyboards.CB_ACC_ADD:
            await event.answer()
            old = state.clear(event.sender_id)
            if isinstance(old, AddAccountConversation):
                await _discard_conversation(old, remove_session=True)
            state.start_add(event.sender_id)
            await event.edit(
                "➕ <b>افزودن اکانت جدید</b>\n\n"
                "یک <b>نام سشن</b> برای این اکانت بفرست "
                "(حروف انگلیسی/عدد/<code>_</code>/<code>-</code>، ۲ تا ۳۲ کاراکتر):",
                buttons=keyboards.cancel_only(),
                parse_mode="html",
            )

        elif data == keyboards.CB_ACC_CANCEL:
            conv = state.clear(event.sender_id)
            await event.answer("لغو شد.")
            if isinstance(conv, AddAccountConversation):
                await _discard_conversation(conv, remove_session=True)
                await _show_accounts_menu(event)
            elif isinstance(conv, SetChannelConversation):
                await _show_numbers_menu(event)
            else:
                await _send_main_menu(event, edit=True)

        # -- numbers / channel submenu --
        elif data == keyboards.CB_NUMBERS:
            await event.answer()
            await _show_numbers_menu(event)

        elif data == keyboards.CB_CH_SET:
            await event.answer()
            old = state.clear(event.sender_id)
            if isinstance(old, AddAccountConversation):
                await _discard_conversation(old, remove_session=True)
            state.start_set_channel(event.sender_id)
            await event.edit(
                "🔧 <b>تنظیم کانال شماره‌ها</b>\n\n"
                "شناسه‌ی کانال را بفرست:\n"
                "• یوزرنیم عمومی مثل <code>@mychannel</code>\n"
                "• آیدی عددی مثل <code>-1001234567890</code>\n"
                "• یا لینک دعوت\n\n"
                "<i>اکانت‌ها باید به این کانال دسترسی داشته باشند.</i>",
                buttons=keyboards.cancel_only(),
                parse_mode="html",
            )

        elif data == keyboards.CB_CH_JOINALL:
            if not coordinator.has_channel:
                await event.answer("اول کانال را تنظیم کن.", alert=True)
                return
            await event.answer()
            await event.edit("⏳ در حال عضو کردن اکانت‌ها در کانال…", parse_mode="html")
            summary = await coordinator.join_all()
            text = (
                "🔗 <b>نتیجه‌ی عضویت</b>\n\n"
                f"✅ موفق: <b>{summary.joined}</b>\n"
                f"❌ ناموفق: <b>{summary.failed}</b>"
            )
            if summary.failures:
                shown = ", ".join(html.escape(n) for n in summary.failures[:10])
                text += f"\n\nناموفق‌ها: {shown}"
            if summary.joined == 0 and summary.failed == 0:
                text += "\n\nℹ️ هیچ اکانتی ثبت نشده است."
            await event.edit(text, buttons=keyboards.numbers_back(), parse_mode="html")

        elif data == keyboards.CB_NUM_READ:
            if not coordinator.has_channel:
                await event.answer("اول کانال را تنظیم کن.", alert=True)
                return
            await event.answer()
            await event.edit("⏳ در حال خواندن پیام‌های کانال…", parse_mode="html")
            results = await coordinator.read_all()
            if not results:
                text = "ℹ️ هیچ اکانتی برای خواندن ثبت نشده است."
            else:
                total_new = sum(r.new_numbers for r in results if r.ok)
                lines = []
                for r in results:
                    if r.ok:
                        lines.append(
                            f"• <b>{html.escape(r.account)}</b>: "
                            f"{r.new_numbers} جدید (اسکن {r.scanned})"
                        )
                    else:
                        lines.append(
                            f"• <b>{html.escape(r.account)}</b>: ⚠️ {html.escape(r.error or 'خطا')}"
                        )
                text = (
                    "📥 <b>خواندن کامل شد</b>\n\n"
                    f"مجموع شماره‌های جدید: <b>{total_new}</b>\n\n" + "\n".join(lines)
                )
            await event.edit(text, buttons=keyboards.numbers_back(), parse_mode="html")

        elif data == keyboards.CB_NUM_STATS:
            await event.answer()
            counts = await db.counts_by_status()
            total = sum(counts.values())
            text = (
                "📊 <b>آمار شماره‌ها</b>\n\n"
                f"⏳ pending: <b>{counts.get('pending', 0)}</b>\n"
                f"🔄 used: <b>{counts.get('used', 0)}</b>\n"
                f"❓ unknown: <b>{counts.get('unknown', 0)}</b>\n"
                f"✅ completed: <b>{counts.get('completed', 0)}</b>\n\n"
                f"🔢 کل: <b>{total}</b>"
            )
            await event.edit(text, buttons=keyboards.numbers_back(), parse_mode="html")

        # -- campaign submenu --
        elif data == keyboards.CB_CAMPAIGN:
            await event.answer()
            # If we came here via the ❌ لغو button, drop any pending input state.
            if isinstance(
                state.get(event.sender_id),
                (
                    SetVoiceDelayConversation,
                    SetReportChannelConversation,
                    CollectMediaConversation,
                ),
            ):
                state.clear(event.sender_id)
            await _show_campaign_menu(event)

        elif data == keyboards.CB_CMP_MEDIA:
            await event.answer()
            state.clear(event.sender_id)
            state.start_collect_media(event.sender_id)
            engine.media.begin_collection()
            await event.edit(
                "🎙 <b>تنظیم مدیا</b>\n\n"
                "ویس، تصویر و حتی <b>متن</b> را همین‌جا برای بات بفرست/فوروارد کن.\n"
                "هر چیزی که بفرستی <b>به همان ترتیب</b> ذخیره می‌شود و بعد از سلام، "
                "دقیقاً به همین ترتیب برای هر نفر ارسال می‌شود.\n\n"
                "وقتی تمام شد، «✅ پایان و ذخیره» را بزن.\n"
                "<i>مدیای قبلی پاک شد و مجموعه‌ی جدید ساخته می‌شود.</i>",
                buttons=keyboards.media_collect(),
                parse_mode="html",
            )

        elif data == keyboards.CB_CMP_MEDIA_DONE:
            await event.answer()
            if isinstance(state.get(event.sender_id), CollectMediaConversation):
                result = engine.media.commit_collection()
                state.clear(event.sender_id)
                await event.edit(
                    f"✅ مدیا ذخیره شد: <b>{result['voices']}</b> ویس و "
                    f"<b>{result['images']}</b> تصویر.",
                    buttons=keyboards.campaign_back(),
                    parse_mode="html",
                )
            else:
                await _show_campaign_menu(event)

        elif data == keyboards.CB_CMP_START:
            # Explicit confirmation before launching ("با تایید من").
            await event.answer()
            accounts = await store.list()
            active = sum(1 for a in accounts if a.get("status") == "active")
            counts = await db.counts_by_status()
            await event.edit(
                "▶️ <b>شروع کمپین</b>\n\n"
                f"👤 اکانت‌های فعال: <b>{active}</b>\n"
                f"⏳ شماره‌های در صف: <b>{counts.get('pending', 0)}</b>\n\n"
                "بعد از تأیید، هر اکانت مستقل شروع به ارسال می‌کند "
                "(فقط ۶ صبح تا ۱۲ شب به وقت ایران).\nمطمئنی؟",
                buttons=keyboards.confirm_start(),
                parse_mode="html",
            )

        elif data == keyboards.CB_CMP_START_OK:
            await event.answer()
            # Ensure channel edits + admin notifications have the bot client.
            engine.bot_client = client
            engine.admin_id = settings.admin_id
            coordinator.bot_client = client
            result = await engine.start()
            if result.get("error"):
                text = f"⚠️ {html.escape(result['error'])}"
            else:
                text = (
                    f"▶️ کمپین شروع شد با <b>{result['started']}</b> اکانت.\n\n"
                    "ارسال‌ها فقط در بازه‌ی ۶ صبح تا ۱۲ شب (به وقت ایران) انجام می‌شوند."
                )
            await event.edit(text, buttons=keyboards.campaign_back(), parse_mode="html")

        elif data == keyboards.CB_CMP_VOICE_DELAY:
            await event.answer()
            state.clear(event.sender_id)
            state.start_set_voice_delay(event.sender_id)
            low, high = engine.cfg.voice_delay()
            await event.edit(
                "⏱ <b>تنظیم زمان ارسال ویس</b>\n\n"
                f"مقدار فعلی: بین <b>{low // 60}</b> تا <b>{high // 60}</b> دقیقه.\n\n"
                "دو عدد (دقیقه) بفرست: «حداقل حداکثر»\nمثال: <code>15 120</code>",
                buttons=keyboards.cancel_campaign(),
                parse_mode="html",
            )

        elif data == keyboards.CB_CMP_REPORT_CH:
            await event.answer()
            state.clear(event.sender_id)
            state.start_set_report_channel(event.sender_id)
            current = coordinator.report_channel_id or "(تنظیم‌نشده)"
            await event.edit(
                "📮 <b>تنظیم کانال گزارش</b>\n\n"
                f"مقدار فعلی: <code>{html.escape(current)}</code>\n\n"
                "شناسه‌ی کانال گزارش را بفرست (@username یا id عددی یا لینک).\n"
                "برای حذف، «-» بفرست.\n\n"
                "<i>بات باید در آن کانال ادمین باشد.</i>",
                buttons=keyboards.cancel_campaign(),
                parse_mode="html",
            )

        elif data == keyboards.CB_CMP_BACKUP:
            await event.answer("در حال ارسال بکاپ…")
            ok = await reporter.send_backup(reason="دستی")
            await event.edit(
                "💾 بکاپ برای ادمین ارسال شد." if ok else "⚠️ بکاپی برای ارسال یافت نشد.",
                buttons=keyboards.campaign_back(),
                parse_mode="html",
            )

        elif data == keyboards.CB_CMP_REPORT:
            await event.answer("در حال ارسال گزارش…")
            await reporter.send_daily_report()
            await event.edit(
                "📈 گزارش برای ادمین ارسال شد.",
                buttons=keyboards.campaign_back(),
                parse_mode="html",
            )

        elif data == keyboards.CB_CMP_STOP:
            await event.answer("در حال توقف…")
            await engine.stop()
            await event.edit(
                "⏸ کمپین متوقف شد.",
                buttons=keyboards.campaign_back(),
                parse_mode="html",
            )

        elif data == keyboards.CB_CMP_STATUS:
            await event.answer()
            st = engine.status()
            counts = await db.counts_by_status()
            lines = [
                "📊 <b>وضعیت کمپین</b>\n",
                f"وضعیت: <b>{'🟢 در حال اجرا' if st['running'] else '⛔️ متوقف'}</b>",
                f"👷 workerهای فعال: <b>{st['workers']}</b>\n",
                "<b>صف شماره‌ها:</b>",
                f"⏳ pending: <b>{counts.get('pending', 0)}</b>",
                f"🔄 used: <b>{counts.get('used', 0)}</b>",
                f"✅ completed: <b>{counts.get('completed', 0)}</b>",
                f"❓ unknown: <b>{counts.get('unknown', 0)}</b>",
            ]
            if st["stats"]:
                lines.append("\n<b>هر اکانت (این اجرا):</b>")
                for acc_phone, s in st["stats"].items():
                    lines.append(
                        f"• <code>{html.escape(acc_phone)}</code>: "
                        f"✅ {s['sent']} | ❓ {s['unknown']} | ⚠️ {s['failed']}"
                    )
            await event.edit(
                "\n".join(lines), buttons=keyboards.campaign_back(), parse_mode="html"
            )

        # -- per-account actions --
        elif data_str.startswith(keyboards.P_ACC_REMOVE_OK):
            name = data_str[len(keyboards.P_ACC_REMOVE_OK):]
            await event.answer("در حال حذف…")
            await manager.logout_and_remove(settings, name)
            removed = await store.remove(name)
            msg = "🗑 اکانت حذف شد." if removed else "اکانت در لیست پیدا نشد."
            await event.edit(msg, buttons=keyboards.accounts_menu(), parse_mode="html")
            log.info("Account '{}' removed by admin.", name)

        elif data_str.startswith(keyboards.P_ACC_REMOVE):
            name = data_str[len(keyboards.P_ACC_REMOVE):]
            await event.answer()
            await event.edit(
                f"⚠️ آیا از حذف اکانت <b>{html.escape(name)}</b> مطمئنی؟\n"
                "این کار اکانت را log out کرده و فایل سشن را حذف می‌کند.",
                buttons=keyboards.confirm_remove(name),
                parse_mode="html",
            )

        elif data_str.startswith(keyboards.P_ACC_VIEW):
            name = data_str[len(keyboards.P_ACC_VIEW):]
            await event.answer()
            acc = await store.get(name)
            if acc is None:
                await event.edit(
                    "اکانت پیدا نشد (شاید حذف شده).",
                    buttons=keyboards.accounts_menu(),
                )
                return
            status = acc.get("status", "inactive")
            try:
                status = "active" if await manager.verify_session(settings, name) else "inactive"
                if status != acc.get("status"):
                    await store.update_status(name, status)
            except Exception:
                log.warning("Live status check failed for {}", name)
            await event.edit(
                _account_detail_text(acc, status),
                buttons=keyboards.account_detail(name),
                parse_mode="html",
            )

        else:
            await event.answer("گزینه‌ی ناشناخته.", alert=False)

        raise events.StopPropagation

    # --- Fallback for any other admin text -------------------------------- #
    @client.on(events.NewMessage)
    @admin_only
    async def _fallback(event):
        if event.raw_text.startswith("/"):
            return
        await event.respond(
            "از /menu برای باز کردن منوی اصلی استفاده کن.",
            buttons=keyboards.main_menu(),
        )

    log.info("Handlers registered (admin_id={}).", settings.admin_id)
