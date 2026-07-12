"""Telethon user-session lifecycle helpers.

These are thin, stateless helpers around a Telethon ``TelegramClient`` bound to
a *user* account (not the bot). Each account gets its own session file at
``sessions/<session_name>.session``.

The interactive login is split into three steps so the bot can drive it across
several chat messages:

    build_client()  ->  send_login_code()  ->  sign_in_with_code()
                                                    └─ (2FA) sign_in_with_password()

The live client returned by ``build_client`` must stay connected between
``send_login_code`` and ``sign_in_*`` — the conversation state keeps a reference
to it for exactly that reason.
"""

from __future__ import annotations

from pathlib import Path

from telethon import TelegramClient

from config import Settings
from utils import get_logger

log = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
SESSIONS_DIR = BASE_DIR / "sessions"


def session_path(session_name: str) -> Path:
    return SESSIONS_DIR / f"{session_name}.session"


def session_exists(session_name: str) -> bool:
    return session_path(session_name).exists()


def build_client(settings: Settings, session_name: str) -> TelegramClient:
    """Create (but do not connect) a Telethon client for a user account."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        session=str(SESSIONS_DIR / session_name),
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        connection_retries=3,
        retry_delay=2,
    )


async def send_login_code(client: TelegramClient, phone: str) -> str:
    """Connect if needed and request an SMS/app login code.

    Returns the ``phone_code_hash`` required to complete sign-in.
    """
    if not client.is_connected():
        await client.connect()
    sent = await client.send_code_request(phone)
    log.debug("Login code requested for {}", phone)
    return sent.phone_code_hash


async def sign_in_with_code(
    client: TelegramClient, phone: str, code: str, phone_code_hash: str
):
    """Complete sign-in with the received code.

    May raise ``SessionPasswordNeededError`` if the account has 2FA enabled.
    """
    return await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)


async def sign_in_with_password(client: TelegramClient, password: str):
    """Complete sign-in for accounts protected by a 2FA (cloud) password."""
    return await client.sign_in(password=password)


async def verify_session(settings: Settings, session_name: str) -> bool:
    """Connect using an existing session file and report authorization state."""
    client = build_client(settings, session_name)
    try:
        await client.connect()
        return await client.is_user_authorized()
    except Exception:
        log.exception("verify_session failed for {}", session_name)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def remove_session_file(session_name: str) -> bool:
    """Delete the session file(s) for an account. Returns True if anything went."""
    removed = False
    base = session_path(session_name)
    for candidate in (base, base.with_name(base.name + "-journal")):
        try:
            if candidate.exists():
                candidate.unlink()
                removed = True
        except OSError:
            log.exception("Failed to delete session file {}", candidate)
    if removed:
        log.info("Session file removed for {}", session_name)
    return removed


async def logout_and_remove(settings: Settings, session_name: str) -> None:
    """Best-effort remote log-out, then delete the local session file."""
    client = build_client(settings, session_name)
    try:
        await client.connect()
        if await client.is_user_authorized():
            await client.log_out()  # invalidates the session server-side
            log.info("Logged out account {} remotely", session_name)
    except Exception:
        log.warning("Remote log-out failed for {} (removing local file anyway)", session_name)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    remove_session_file(session_name)
