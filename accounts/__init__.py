"""User-account (session) management.

Provides:
  * ``AccountStore``       — async JSON persistence of account metadata.
  * ``manager``            — Telethon session lifecycle helpers (login/verify).
  * ``AccountCoordinator`` — cross-account orchestration (join channel, read
    numbers ascending, coordinate cursors).

The bot conversation flow that drives these lives in ``bot/handlers.py``.
"""

from . import manager
from .coordinator import AccountCoordinator
from .store import AccountStore

__all__ = ["AccountStore", "AccountCoordinator", "manager"]
