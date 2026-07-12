"""Configuration system for the Telegram automation tool.

Responsibilities
----------------
1. Load settings from a ``.env`` file (via python-dotenv).
2. On first run (or when required keys are missing), launch a Rich-based
   terminal wizard to collect them interactively.
3. Persist collected values back to ``.env`` with strict file permissions.
4. Validate everything into a typed, immutable ``Settings`` object.

Required keys: API_ID, API_HASH, BOT_TOKEN, ADMIN_ID
(API_ID / API_HASH are needed by Telethon's MTProto layer — even for bots.)
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, set_key
from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from utils import get_logger

log = get_logger(__name__)
console = Console()

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

REQUIRED_KEYS = ("API_ID", "API_HASH", "BOT_TOKEN", "ADMIN_ID")


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable, validated application settings."""

    api_id: int
    api_hash: str
    bot_token: str
    admin_id: int
    log_level: str = "INFO"
    # Numbers channel (PNUMBERS). Optional: may be set later from the bot/terminal.
    # Accepts a @username, a numeric id (e.g. -1001234567890), or an invite link.
    channel_id: str | None = None
    # Optional separate channel where the bot also posts reports/backups/notices.
    report_channel_id: str | None = None

    def masked(self) -> dict[str, str]:
        """Return a display-safe (secret-masked) view of the settings."""

        def mask(value: str, keep: int = 4) -> str:
            value = str(value)
            if len(value) <= keep:
                return "*" * len(value)
            return value[:keep] + "…" + "*" * 4

        return {
            "API_ID": str(self.api_id),
            "API_HASH": mask(self.api_hash),
            "BOT_TOKEN": mask(self.bot_token, keep=6),
            "ADMIN_ID": str(self.admin_id),
            "CHANNEL_ID": self.channel_id or "(تنظیم‌نشده)",
            "REPORT_CHANNEL_ID": self.report_channel_id or "(تنظیم‌نشده)",
            "LOG_LEVEL": self.log_level,
        }


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
class ConfigError(Exception):
    """Raised when configuration values are missing or invalid."""


def _validate_api_id(raw: str | int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"API_ID must be an integer, got: {raw!r}") from exc
    if value <= 0:
        raise ConfigError("API_ID must be a positive integer.")
    return value


def _validate_api_hash(raw: str) -> str:
    value = (raw or "").strip()
    # Telegram api_hash is a 32-char hex string.
    if len(value) < 30:
        raise ConfigError("API_HASH looks too short — check my.telegram.org.")
    return value


def _validate_bot_token(raw: str) -> str:
    value = (raw or "").strip()
    # BotFather tokens look like  123456789:AA...  (numeric id, colon, secret)
    if ":" not in value or not value.split(":", 1)[0].isdigit():
        raise ConfigError("BOT_TOKEN is malformed — expected '<id>:<secret>'.")
    return value


def _validate_admin_id(raw: str | int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"ADMIN_ID must be a numeric user id, got: {raw!r}") from exc
    if value <= 0:
        raise ConfigError("ADMIN_ID must be a positive integer.")
    return value


# --------------------------------------------------------------------------- #
# First-run TUI wizard
# --------------------------------------------------------------------------- #
def _run_setup_wizard(existing: dict[str, str]) -> dict[str, str]:
    """Interactively collect required settings and return them as a dict."""

    console.print()
    console.print(
        Panel.fit(
            Text.from_markup(
                "[bold cyan]Telegram Automation — First-time Setup[/bold cyan]\n"
                "[dim]These values are stored locally in your [white].env[/white] file "
                "and never leave this machine.[/dim]"
            ),
            border_style="cyan",
            padding=(1, 4),
        )
    )
    console.print(
        "\n[bold]Where to find these:[/bold]\n"
        "  • [cyan]API_ID / API_HASH[/cyan] → https://my.telegram.org → "
        "[i]API development tools[/i]\n"
        "  • [cyan]BOT_TOKEN[/cyan]        → Telegram [i]@BotFather[/i] → /newbot\n"
        "  • [cyan]ADMIN_ID[/cyan]         → Telegram [i]@userinfobot[/i] (your numeric id)\n"
    )

    def _default(key: str) -> str | None:
        val = existing.get(key)
        return val if val else None

    while True:
        try:
            api_id = IntPrompt.ask(
                "[bold]API_ID[/bold]",
                default=int(_default("API_ID")) if _default("API_ID") else None,
            )
            api_hash = Prompt.ask(
                "[bold]API_HASH[/bold]",
                default=_default("API_HASH"),
                password=True,
            )
            bot_token = Prompt.ask(
                "[bold]BOT_TOKEN[/bold]",
                default=_default("BOT_TOKEN"),
                password=True,
            )
            admin_id = IntPrompt.ask(
                "[bold]ADMIN_ID[/bold] (your numeric Telegram user id)",
                default=int(_default("ADMIN_ID")) if _default("ADMIN_ID") else None,
            )
            channel_id = Prompt.ask(
                "[bold]CHANNEL_ID[/bold] (اختیاری — @username یا id یا لینک؛ "
                "می‌توانی بعداً از داخل بات هم تنظیم کنی)",
                default=_default("CHANNEL_ID") or "",
            )

            # Validate right away so the user can correct mistakes in-loop.
            collected = {
                "API_ID": str(_validate_api_id(api_id)),
                "API_HASH": _validate_api_hash(api_hash),
                "BOT_TOKEN": _validate_bot_token(bot_token),
                "ADMIN_ID": str(_validate_admin_id(admin_id)),
                "CHANNEL_ID": normalize_channel(channel_id) or "",
                "LOG_LEVEL": existing.get("LOG_LEVEL", "INFO"),
            }
        except ConfigError as exc:
            console.print(f"[bold red]✗ {exc}[/bold red]\n[dim]Let's try again…[/dim]\n")
            continue

        # Confirmation summary
        table = Table(title="Confirm your settings", border_style="green", show_header=True)
        table.add_column("Key", style="cyan", no_wrap=True)
        table.add_column("Value", style="white")
        preview = Settings(
            api_id=int(collected["API_ID"]),
            api_hash=collected["API_HASH"],
            bot_token=collected["BOT_TOKEN"],
            admin_id=int(collected["ADMIN_ID"]),
            channel_id=collected["CHANNEL_ID"] or None,
            log_level=collected["LOG_LEVEL"],
        ).masked()
        for key, value in preview.items():
            table.add_row(key, value)
        console.print()
        console.print(table)

        if Prompt.ask("\n[bold]Save these settings?[/bold]", choices=["y", "n"], default="y") == "y":
            return collected
        console.print("[yellow]Okay, let's re-enter them.[/yellow]\n")


def _persist(values: dict[str, str]) -> None:
    """Write values to .env and lock down file permissions where supported."""
    ENV_PATH.touch(exist_ok=True)
    for key, value in values.items():
        set_key(str(ENV_PATH), key, str(value), quote_mode="never")

    # Best-effort: restrict to owner read/write (no-op / limited on Windows).
    try:
        os.chmod(ENV_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except (OSError, NotImplementedError):
        log.debug("Could not chmod .env (likely on Windows) — skipping.")

    log.info("Settings saved to {}", ENV_PATH.name)


# --------------------------------------------------------------------------- #
# CHANNEL_ID (settable at runtime from the bot or the terminal)
# --------------------------------------------------------------------------- #
def normalize_channel(value: str | None) -> str | None:
    """Trim a channel identifier; return None if empty."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def persist_channel_id(value: str | None) -> str | None:
    """Write CHANNEL_ID to .env (or blank it) and return the normalized value."""
    norm = normalize_channel(value)
    ENV_PATH.touch(exist_ok=True)
    set_key(str(ENV_PATH), "CHANNEL_ID", norm or "", quote_mode="never")
    log.info("CHANNEL_ID updated in .env -> {}", norm or "(cleared)")
    return norm


def persist_report_channel_id(value: str | None) -> str | None:
    """Write REPORT_CHANNEL_ID to .env (or blank it); return the normalized value."""
    norm = normalize_channel(value)
    ENV_PATH.touch(exist_ok=True)
    set_key(str(ENV_PATH), "REPORT_CHANNEL_ID", norm or "", quote_mode="never")
    log.info("REPORT_CHANNEL_ID updated in .env -> {}", norm or "(cleared)")
    return norm


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def load_settings(*, interactive: bool = True) -> Settings:
    """Load settings from environment/.env, running the wizard if needed.

    Precedence: real environment variables > .env file > wizard input.

    Parameters
    ----------
    interactive:
        If True (default) and required keys are missing, launch the TUI wizard.
        If False, raise ``ConfigError`` instead (useful for CI / headless runs).
    """
    file_values = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}

    def resolve(key: str) -> str | None:
        # Environment variables win over the .env file.
        return os.environ.get(key) or file_values.get(key)

    current = {key: resolve(key) for key in REQUIRED_KEYS}
    missing = [key for key, value in current.items() if not value]

    if missing:
        log.warning("Missing required config: {}", ", ".join(missing))
        if not interactive:
            raise ConfigError(
                f"Missing required settings: {', '.join(missing)}. "
                "Set them in .env or run interactively."
            )
        collected = _run_setup_wizard({k: (resolve(k) or "") for k in dotenv_values(ENV_PATH) or {}}
                                      | {k: (v or "") for k, v in current.items()})
        _persist(collected)
        current = collected
    else:
        current["LOG_LEVEL"] = resolve("LOG_LEVEL") or "INFO"

    channel_raw = current["CHANNEL_ID"] if "CHANNEL_ID" in current else resolve("CHANNEL_ID")
    report_raw = resolve("REPORT_CHANNEL_ID")

    # Final validation (also covers values coming straight from .env / env).
    try:
        settings = Settings(
            api_id=_validate_api_id(current["API_ID"]),
            api_hash=_validate_api_hash(current["API_HASH"]),
            bot_token=_validate_bot_token(current["BOT_TOKEN"]),
            admin_id=_validate_admin_id(current["ADMIN_ID"]),
            channel_id=normalize_channel(channel_raw),
            report_channel_id=normalize_channel(report_raw),
            log_level=(current.get("LOG_LEVEL") or "INFO").upper(),
        )
    except ConfigError:
        raise
    except Exception as exc:  # defensive: never leak a raw traceback with secrets
        raise ConfigError(f"Invalid configuration: {exc}") from exc

    log.debug("Settings loaded and validated successfully.")
    return settings
