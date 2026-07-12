"""Entry point for the Telegram automation tool.

Usage:
    python main.py                             # run the bot (first run = wizard)
    python main.py set-channel <VALUE>         # set CHANNEL_ID from the terminal
    python main.py set-report-channel <VALUE>  # set REPORT_CHANNEL_ID
    (omit <VALUE> to be prompted interactively)

Flow (run mode):
    1. Configure logging.
    2. Load settings (runs the first-time TUI wizard if needed).
    3. Boot the central admin bot and run until interrupted.
"""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console
from rich.prompt import Prompt

from config import (
    ConfigError,
    load_settings,
    persist_channel_id,
    persist_report_channel_id,
)
from utils import get_logger, setup_logging

console = Console()


def _set_channel_cli(args: list[str]) -> int:
    """Handle `python main.py set-channel [value]` from the terminal."""
    value = args[0] if args else Prompt.ask(
        "CHANNEL_ID (@username / -100... / invite link)"
    )
    norm = persist_channel_id(value)
    if norm:
        console.print(f"[green]✓ CHANNEL_ID set to[/green] {norm}")
    else:
        console.print("[yellow]CHANNEL_ID cleared.[/yellow]")
    console.print("[dim]Restart the bot for the change to take effect.[/dim]")
    return 0


def _set_report_channel_cli(args: list[str]) -> int:
    """Handle `python main.py set-report-channel [value]` from the terminal."""
    value = args[0] if args else Prompt.ask(
        "REPORT_CHANNEL_ID (@username / -100... / invite link, '-' to clear)"
    )
    if value.strip() in {"-", "off", ""}:
        value = None
    norm = persist_report_channel_id(value)
    if norm:
        console.print(f"[green]✓ REPORT_CHANNEL_ID set to[/green] {norm}")
    else:
        console.print("[yellow]REPORT_CHANNEL_ID cleared.[/yellow]")
    console.print("[dim]Restart the bot for the change to take effect.[/dim]")
    return 0


async def _main_async() -> int:
    # 1) Settings (may launch the interactive wizard on first run).
    #    Under a service manager (no TTY) we can't prompt — fail clearly instead.
    interactive = sys.stdin.isatty()
    try:
        settings = load_settings(interactive=interactive)
    except ConfigError as exc:
        console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        if not interactive:
            console.print(
                "[yellow]No terminal detected. Configure .env first "
                "(run `python main.py` once interactively, or set env vars).[/yellow]"
            )
        return 2

    # 2) Reconfigure logging with the user's chosen level.
    setup_logging(level=settings.log_level)
    log = get_logger("main")
    log.info("Starting Telegram Automation…")

    # 3) Boot the bot. Imported here so logging is ready first.
    from bot import BotApp

    app = BotApp(settings)
    try:
        await app.start()
        await app.run_until_disconnected()
    except RuntimeError as exc:
        log.error("Startup failed: {}", exc)
        console.print(f"[bold red]✗ {exc}[/bold red]")
        return 1
    except asyncio.CancelledError:
        log.info("Cancelled — shutting down.")
    finally:
        await app.stop()

    return 0


def cli() -> None:
    """Synchronous console-script wrapper (used by `tg-automation`)."""
    # Ensure basic logging exists even before settings are loaded.
    setup_logging()

    argv = sys.argv[1:]
    if argv and argv[0] == "set-channel":
        sys.exit(_set_channel_cli(argv[1:]))
    if argv and argv[0] == "set-report-channel":
        sys.exit(_set_report_channel_cli(argv[1:]))

    try:
        exit_code = asyncio.run(_main_async())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user. Bye![/yellow]")
        exit_code = 0
    sys.exit(exit_code)


if __name__ == "__main__":
    cli()
