"""Campaign media library — an ORDERED set of items the admin forwards.

The admin forwards voice/image messages (and can type text) to the bot during
"Set media". Each item is saved in order, so phase 2 sends them back in exactly
the same order the admin sent them:

    manifest = {"items": [
        {"type": "voice", "path": ".../voice_1.ogg"},
        {"type": "text",  "text": "..."},
        {"type": "image", "path": ".../img_1.jpg"},
    ]}

Files live in ``data/media/`` so sending never touches the channel and there is
no "Forwarded from" header.
"""

from __future__ import annotations

import json
from pathlib import Path

from utils import get_logger

log = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
MEDIA_DIR = BASE_DIR / "data" / "media"
MANIFEST_PATH = MEDIA_DIR / "manifest.json"


class MediaLibrary:
    """Ordered media set, collected by forwarding messages to the bot."""

    def __init__(self) -> None:
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        # Buffer used while the admin is forwarding media to the bot.
        self._buf_items: list[dict] = []

    # ------------------------------------------------------------------ #
    def load(self) -> list[dict]:
        """Return the ordered items, dropping any whose files vanished."""
        if not MANIFEST_PATH.exists():
            return []
        try:
            data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.exception("Could not read media manifest")
            return []

        # New format: {"items": [...]}. Old format: {"voices": [...], "images": [...]}.
        if isinstance(data, dict) and "items" in data:
            raw_items = data.get("items", [])
        else:
            raw_items = [{"type": "voice", "path": p} for p in data.get("voices", [])]
            raw_items += [{"type": "image", "path": p} for p in data.get("images", [])]

        items: list[dict] = []
        for item in raw_items:
            kind = item.get("type")
            if kind == "text":
                if (item.get("text") or "").strip():
                    items.append({"type": "text", "text": item["text"]})
            elif kind in ("voice", "image") and Path(item.get("path", "")).exists():
                items.append({"type": kind, "path": item["path"]})
        return items

    def counts(self) -> dict:
        c = {"voices": 0, "images": 0, "texts": 0}
        for item in self.load():
            if item["type"] == "voice":
                c["voices"] += 1
            elif item["type"] == "image":
                c["images"] += 1
            elif item["type"] == "text":
                c["texts"] += 1
        return c

    def is_ready(self) -> bool:
        return bool(self.load())

    # ------------------------------------------------------------------ #
    # Collection by forwarding media to the bot (private chat)
    # ------------------------------------------------------------------ #
    def begin_collection(self) -> None:
        """Start a fresh media set: wipe old files and reset the buffer."""
        for path in MEDIA_DIR.glob("*"):
            try:
                path.unlink()
            except OSError:
                log.debug("Could not delete old media file {}", path)
        self._buf_items = []

    async def add_from_message(self, message) -> str | None:
        """Download a forwarded voice/image and append it in order.

        Returns 'voice' / 'image', or None if the message has no supported media.
        """
        doc = getattr(message, "document", None)
        mime = getattr(doc, "mime_type", "") or ""
        n = len(self._buf_items) + 1
        if message.voice or message.audio or mime.startswith("audio/"):
            path = MEDIA_DIR / f"voice_{n}.ogg"
            await message.download_media(file=str(path))
            self._buf_items.append({"type": "voice", "path": str(path)})
            return "voice"
        if message.photo or mime.startswith("image/"):
            path = MEDIA_DIR / f"img_{n}.jpg"
            await message.download_media(file=str(path))
            self._buf_items.append({"type": "image", "path": str(path)})
            return "image"
        return None

    def add_text(self, text: str) -> str:
        """Append a text item in order."""
        self._buf_items.append({"type": "text", "text": text})
        return "text"

    def commit_collection(self) -> dict:
        """Write the manifest from the collected buffer (in order)."""
        MANIFEST_PATH.write_text(
            json.dumps({"items": self._buf_items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result = self.collected
        log.info("Media committed via forward: {}", result)
        return result

    @property
    def collected(self) -> dict:
        c = {"voices": 0, "images": 0, "texts": 0}
        for item in self._buf_items:
            c[{"voice": "voices", "image": "images", "text": "texts"}[item["type"]]] += 1
        return c
