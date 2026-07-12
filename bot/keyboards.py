"""Inline keyboard layouts for the admin control panel.

Callback data is namespaced (``menu:``, ``acc:``) so that features can add
their own buttons without collisions. Per-account buttons embed the session
name after the action, e.g. ``acc:view:<name>``. Session names are validated
elsewhere to be short and ``[A-Za-z0-9_-]`` only, so they never contain ``:``
and always fit inside Telegram's 64-byte callback-data limit.
"""

from __future__ import annotations

from typing import Any

from telethon import Button

# --- Main menu ------------------------------------------------------------- #
CB_STATUS = b"menu:status"
CB_ACCOUNTS = b"menu:accounts"
CB_NUMBERS = b"menu:numbers"
CB_CAMPAIGN = b"menu:campaign"
CB_SETTINGS = b"menu:settings"
CB_HELP = b"menu:help"
CB_HOME = b"menu:home"

# --- Campaign submenu ------------------------------------------------------ #
CB_CMP_START = b"cmp:start"
CB_CMP_START_OK = b"cmp:startok"
CB_CMP_STOP = b"cmp:stop"
CB_CMP_MEDIA = b"cmp:media"
CB_CMP_STATUS = b"cmp:status"
CB_CMP_VOICE_DELAY = b"cmp:vdelay"
CB_CMP_BACKUP = b"cmp:backup"
CB_CMP_REPORT = b"cmp:report"
CB_CMP_REPORT_CH = b"cmp:reportch"

# --- Accounts submenu ------------------------------------------------------ #
CB_ACC_LIST = b"acc:list"
CB_ACC_ADD = b"acc:add"
CB_ACC_CANCEL = b"acc:cancel"

# --- Numbers / channel submenu --------------------------------------------- #
CB_CH_SET = b"num:setchannel"
CB_CH_JOINALL = b"num:joinall"
CB_NUM_READ = b"num:read"
CB_NUM_STATS = b"num:stats"

# Prefixes for per-account actions (name appended)
P_ACC_VIEW = "acc:view:"
P_ACC_REMOVE = "acc:rm:"
P_ACC_REMOVE_OK = "acc:rmok:"


def main_menu() -> list[list[Button]]:
    """The primary admin menu shown after /start."""
    return [
        [
            Button.inline("📊 وضعیت", CB_STATUS),
            Button.inline("👤 اکانت‌ها", CB_ACCOUNTS),
        ],
        [
            Button.inline("📇 شماره‌ها", CB_NUMBERS),
            Button.inline("🚀 کمپین", CB_CAMPAIGN),
        ],
        [
            Button.inline("⚙️ تنظیمات", CB_SETTINGS),
            Button.inline("❓ راهنما", CB_HELP),
        ],
    ]


def campaign_menu(running: bool) -> list[list[Button]]:
    """Campaign control submenu."""
    toggle = (
        Button.inline("⏸ توقف کمپین", CB_CMP_STOP)
        if running
        else Button.inline("▶️ شروع کمپین", CB_CMP_START)
    )
    return [
        [toggle],
        [Button.inline("🔄 به‌روزرسانی مدیا از کانال", CB_CMP_MEDIA)],
        [Button.inline("⏱ تنظیم زمان ارسال ویس", CB_CMP_VOICE_DELAY)],
        [Button.inline("📮 تنظیم کانال گزارش", CB_CMP_REPORT_CH)],
        [Button.inline("📊 وضعیت کمپین", CB_CMP_STATUS)],
        [
            Button.inline("💾 بکاپ الان", CB_CMP_BACKUP),
            Button.inline("📈 گزارش الان", CB_CMP_REPORT),
        ],
        [Button.inline("⬅️ بازگشت به منو", CB_HOME)],
    ]


def campaign_back() -> list[list[Button]]:
    return [[Button.inline("⬅️ بازگشت", CB_CAMPAIGN)]]


def confirm_start() -> list[list[Button]]:
    """Explicit confirmation before launching the campaign ('با تایید من')."""
    return [
        [
            Button.inline("✅ بله، شروع کن", CB_CMP_START_OK),
            Button.inline("❌ انصراف", CB_CAMPAIGN),
        ]
    ]


def cancel_campaign() -> list[list[Button]]:
    """Cancel button used inside campaign conversations (e.g. voice-delay)."""
    return [[Button.inline("❌ لغو", CB_CAMPAIGN)]]


def numbers_menu(has_channel: bool) -> list[list[Button]]:
    """Numbers / channel management submenu."""
    set_label = "🔧 تغییر کانال" if has_channel else "🔧 تنظیم کانال"
    return [
        [Button.inline(set_label, CB_CH_SET)],
        [Button.inline("🔗 عضویت همه‌ی اکانت‌ها در کانال", CB_CH_JOINALL)],
        [Button.inline("📥 خواندن شماره‌ها از کانال", CB_NUM_READ)],
        [Button.inline("📊 آمار شماره‌ها", CB_NUM_STATS)],
        [Button.inline("⬅️ بازگشت به منو", CB_HOME)],
    ]


def numbers_back() -> list[list[Button]]:
    """Back to the numbers submenu."""
    return [[Button.inline("⬅️ بازگشت", CB_NUMBERS)]]


def back_button() -> list[list[Button]]:
    """A single 'back to main menu' row for sub-screens."""
    return [[Button.inline("⬅️ بازگشت به منو", CB_HOME)]]


# --- Accounts ------------------------------------------------------------- #
def accounts_menu() -> list[list[Button]]:
    """Landing screen for account management."""
    return [
        [Button.inline("📋 لیست اکانت‌ها", CB_ACC_LIST)],
        [Button.inline("➕ افزودن اکانت", CB_ACC_ADD)],
        [Button.inline("⬅️ بازگشت به منو", CB_HOME)],
    ]


def accounts_list(accounts: list[dict[str, Any]]) -> list[list[Button]]:
    """One button per account (status emoji + name), then actions."""
    rows: list[list[Button]] = []
    for acc in accounts:
        name = acc.get("session_name", "?")
        emoji = "🟢" if acc.get("status") == "active" else "🔴"
        rows.append([Button.inline(f"{emoji} {name}", f"{P_ACC_VIEW}{name}".encode())])
    rows.append(
        [
            Button.inline("➕ افزودن", CB_ACC_ADD),
            Button.inline("⬅️ بازگشت", CB_ACCOUNTS),
        ]
    )
    return rows


def account_detail(session_name: str) -> list[list[Button]]:
    """Actions available on a single account's detail screen."""
    return [
        [Button.inline("🗑 حذف این اکانت", f"{P_ACC_REMOVE}{session_name}".encode())],
        [Button.inline("⬅️ بازگشت به لیست", CB_ACC_LIST)],
    ]


def confirm_remove(session_name: str) -> list[list[Button]]:
    """Yes/No confirmation for a destructive account removal."""
    return [
        [
            Button.inline("✅ بله، حذف کن", f"{P_ACC_REMOVE_OK}{session_name}".encode()),
            Button.inline("❌ انصراف", f"{P_ACC_VIEW}{session_name}".encode()),
        ]
    ]


def cancel_only() -> list[list[Button]]:
    """A lone cancel button shown during the add-account conversation."""
    return [[Button.inline("❌ لغو", CB_ACC_CANCEL)]]
