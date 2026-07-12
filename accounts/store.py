"""Async JSON storage for Telegram account metadata.

The store keeps a single ``data/accounts.json`` file with the shape::

    {
      "accounts": [
        {
          "session_name": "acc1",
          "phone": "+98912...",
          "status": "active",           # active | inactive
          "user_id": 111,
          "username": "someone",
          "first_name": "Ali",
          "added_at": "2026-07-11T12:00:00+00:00"
        }
      ]
    }

Writes are atomic (temp file + ``os.replace``) and serialized behind an
``asyncio.Lock`` so concurrent handler callbacks can't corrupt the file.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import aiofiles

from utils import get_logger

log = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STORE_PATH = DATA_DIR / "accounts.json"


class AccountStore:
    """Thread-safe (single event loop) async JSON store for accounts."""

    def __init__(self, path: Path | str = STORE_PATH) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ---- low-level (call only while holding, or not needing, the lock) ---- #
    async def _read_raw(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"accounts": []}
        try:
            async with aiofiles.open(self._path, "r", encoding="utf-8") as fh:
                content = await fh.read()
            if not content.strip():
                return {"accounts": []}
            data = json.loads(content)
            if not isinstance(data, dict) or "accounts" not in data:
                raise ValueError("unexpected structure")
            return data
        except (json.JSONDecodeError, ValueError) as exc:
            # Never crash on a corrupt file — back it up and start clean.
            backup = self._path.with_suffix(".corrupt")
            try:
                os.replace(self._path, backup)
                log.error("accounts.json was corrupt ({}); backed up to {}", exc, backup.name)
            except OSError:
                log.exception("Failed to back up corrupt accounts.json")
            return {"accounts": []}

    async def _write_raw(self, data: dict[str, Any]) -> None:
        tmp = self._path.with_suffix(".tmp")
        async with aiofiles.open(tmp, "w", encoding="utf-8") as fh:
            await fh.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, self._path)  # atomic on the same filesystem

    # ---- public API ------------------------------------------------------- #
    async def list(self) -> list[dict[str, Any]]:
        async with self._lock:
            data = await self._read_raw()
        return list(data.get("accounts", []))

    async def get(self, session_name: str) -> dict[str, Any] | None:
        for acc in await self.list():
            if acc.get("session_name") == session_name:
                return acc
        return None

    async def exists(self, session_name: str) -> bool:
        return await self.get(session_name) is not None

    async def exists_phone(self, phone: str) -> bool:
        return any(acc.get("phone") == phone for acc in await self.list())

    async def add(self, account: dict[str, Any]) -> None:
        async with self._lock:
            data = await self._read_raw()
            data.setdefault("accounts", []).append(account)
            await self._write_raw(data)
        log.info("Account stored: {} ({})", account.get("session_name"), account.get("phone"))

    async def remove(self, session_name: str) -> bool:
        async with self._lock:
            data = await self._read_raw()
            accounts = data.get("accounts", [])
            remaining = [a for a in accounts if a.get("session_name") != session_name]
            changed = len(remaining) != len(accounts)
            if changed:
                data["accounts"] = remaining
                await self._write_raw(data)
        if changed:
            log.info("Account removed from store: {}", session_name)
        return changed

    async def update_status(self, session_name: str, status: str) -> bool:
        async with self._lock:
            data = await self._read_raw()
            found = False
            for acc in data.get("accounts", []):
                if acc.get("session_name") == session_name:
                    acc["status"] = status
                    found = True
            if found:
                await self._write_raw(data)
        return found

    async def count(self) -> int:
        return len(await self.list())
