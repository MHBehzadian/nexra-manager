"""Centralized logging configuration built on top of loguru.

Provides:
  * A colored, human-friendly console sink (via Rich-compatible formatting).
  * A rotating file sink under ``logs/`` with retention + compression.
  * A single ``get_logger()`` helper so every module shares one config.

Usage
-----
    from utils import setup_logging, get_logger

    setup_logging(level="INFO")          # call once, early in main()
    log = get_logger(__name__)
    log.info("hello")
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

# Project-root/logs  (this file lives in project-root/utils/logger.py)
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
    "| <level>{level: <8}</level> "
    "| <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
    "- <level>{message}</level>"
)

_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} "
    "| {name}:{function}:{line} - {message}"
)

_configured = False


def setup_logging(level: str = "INFO") -> None:
    """Configure the global loguru logger. Safe to call more than once."""
    global _configured

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Start from a clean slate so re-configuration doesn't duplicate sinks.
    logger.remove()

    level = (level or "INFO").upper()

    # Console sink
    logger.add(
        sys.stderr,
        level=level,
        format=_CONSOLE_FORMAT,
        colorize=True,
        backtrace=True,
        diagnose=False,  # keep secrets/values out of tracebacks in prod
        enqueue=True,
    )

    # Rotating file sink (all levels from DEBUG up so the file is complete)
    logger.add(
        _LOG_DIR / "app_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        format=_FILE_FORMAT,
        rotation="10 MB",
        retention="14 days",
        compression="zip",
        encoding="utf-8",
        backtrace=True,
        diagnose=False,
        enqueue=True,
    )

    # Dedicated error file for quick incident triage
    logger.add(
        _LOG_DIR / "errors.log",
        level="ERROR",
        format=_FILE_FORMAT,
        rotation="5 MB",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
        backtrace=True,
        diagnose=False,
        enqueue=True,
    )

    _configured = True
    logger.debug("Logging configured (level={})", level)


def get_logger(name: str | None = None):
    """Return a logger bound to ``name`` (falls back to global logger)."""
    if not _configured:
        setup_logging()
    return logger.bind(name=name) if name else logger
