"""Campaign content and timing configuration.

Everything a human might want to tweak about *what* is sent and *when* lives
here: the greeting texts, the random delay ranges, and the Iran-time active
window (06:00–24:00 Asia/Tehran).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore

# --- Greetings (one is chosen at random for the first message) ------------- #
GREETINGS: tuple[str, ...] = (
    "سلام وقت بخیر",
    "سلام وقتتون بخیر باشه",
    "سلام خوب هستین؟ وقت بخیر",
)


def random_greeting() -> str:
    return random.choice(GREETINGS)


# --- Delay ranges (seconds) ------------------------------------------------ #
# Gap between the greeting and the voice/images, per number.
# NOTE: this is the DEFAULT; it can be overridden at runtime from the bot
# (see sender/campaign_config.py).
GREETING_TO_VOICE: tuple[int, int] = (15 * 60, 2 * 60 * 60)  # 15 min – 2 h
# Gap between each individual item (voice/image/text) sent to one customer in
# phase 2, so they don't all arrive at once.
BETWEEN_ITEMS: tuple[int, int] = (30, 2 * 60)  # 30 s – 2 min
# Gap between finishing one number and starting the next, per account.
BETWEEN_NUMBERS: tuple[int, int] = (40 * 60, 2 * 60 * 60)  # 40 min – 2 h
# Global gap between two consecutive numbers dispatched to (different) accounts.
# Round-robin + this gap gives the channel edit time to land, so a late edit
# can't cause the same number to be messaged twice.
DISPATCH_GAP_SECONDS: int = 60  # 1 minute
# How long the dispatcher sleeps when the queue is empty before checking again.
IDLE_POLL_SECONDS: int = 120
# Alert the admin when the pending queue drops to/below each of these counts.
LOW_STOCK_THRESHOLDS: tuple[int, ...] = (50, 20, 10)
# When an account is temporarily limited (e.g. PeerFlood), rest it this long and
# then bring it back into rotation automatically — the limit is usually lifted
# after a few hours, so the account is NOT removed permanently.
ACCOUNT_COOLDOWN_SECONDS: int = 4 * 60 * 60  # 4 hours
# If an account gets this many "no Telegram account" results in a row, it is
# probably contact-import-limited (not that all those numbers lack Telegram):
# cool it down and requeue those numbers for another account. The counter resets
# on any successful resolve, so scattered no-Telegram numbers won't trigger it.
NO_USER_LIMIT_THRESHOLD: int = 15


def random_delay(bounds: tuple[int, int]) -> float:
    """Random delay within ``bounds`` (inclusive), to sub-second resolution."""
    low, high = bounds
    return random.uniform(low, high)


# --- Active window (Iran time) --------------------------------------------- #
ACTIVE_TZ = "Asia/Tehran"
ACTIVE_START_HOUR = 6   # 06:00
ACTIVE_END_HOUR = 24    # midnight (exclusive)


def _tehran_now() -> datetime:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo(ACTIVE_TZ))
    # Fallback: assume system clock is already close enough (best-effort).
    return datetime.now()


def in_active_window(now: datetime | None = None) -> bool:
    """True if the current Tehran time is inside the allowed sending window."""
    now = now or _tehran_now()
    return ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR


def seconds_until_window(now: datetime | None = None) -> float:
    """Seconds until the window next opens (0 if already open)."""
    now = now or _tehran_now()
    if in_active_window(now):
        return 0.0
    target = now.replace(hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max(0.0, (target - now).total_seconds())


# --- Daily report time (Iran) ---------------------------------------------- #
DAILY_REPORT_HOUR = 23  # 23:00 Tehran — near the end of the active window


def seconds_until_daily_report(now: datetime | None = None) -> float:
    """Seconds until the next daily-report time (Tehran ``DAILY_REPORT_HOUR``)."""
    now = now or _tehran_now()
    target = now.replace(hour=DAILY_REPORT_HOUR, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def seconds_until_next_6h(now: datetime | None = None) -> float:
    """Seconds until the next Tehran 6-hour boundary (00, 06, 12, 18)."""
    now = now or _tehran_now()
    nxt = ((now.hour // 6) + 1) * 6
    if nxt >= 24:
        target = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    else:
        target = now.replace(hour=nxt, minute=0, second=0, microsecond=0)
    return max(1.0, (target - now).total_seconds())


def seconds_until_midnight(now: datetime | None = None) -> float:
    """Seconds until the next Tehran 00:00."""
    now = now or _tehran_now()
    target = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def tehran_day_start_utc(now: datetime | None = None) -> datetime:
    """UTC datetime for the start of the current Tehran day (00:00 Tehran)."""
    now = now or _tehran_now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if start.tzinfo is None:  # fallback path without zoneinfo
        return start
    return start.astimezone(timezone.utc)
