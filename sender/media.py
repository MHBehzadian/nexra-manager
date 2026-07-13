"""Channel media library.

The campaign sends the voice message(s) and image(s) that live in the numbers
channel. To make outgoing messages look native (no "Forwarded from" header) and
to keep working even if an account later leaves the channel, media is downloaded
**once** into ``data/media/`` and re-sent from that cache.

A small ``manifest.json`` records the cached file paths so the engine doesn't
need to touch the channel on every send.
"""

from __future__ import annotations

import json
from pathlib import Path

from utils import get_logger

log = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
MEDIA_DIR = BASE_DIR / "data" / "media"
MANIFEST_PATH = MEDIA_DIR / "manifest.json"

# Safety cap so a huge channel doesn't download thousands of photos.
MAX_IMAGES = 20


class MediaLibrary:
    """Downloads and caches the channel's voice/image media."""

    def __init__(self) -> None:
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        # Buffers used while the admin forwards media to the bot.
        self._buf_voices: list[str] = []
        self._buf_images: list[str] = []

    # ------------------------------------------------------------------ #
    def load(self) -> dict:
        """Return the cached manifest: ``{"voices": [...], "images": [...]}``."""
        if not MANIFEST_PATH.exists():
            return {"voices": [], "images": []}
        try:
            data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.exception("Could not read media manifest")
            return {"voices": [], "images": []}
        # Drop any entries whose files vanished.
        voices = [p for p in data.get("voices", []) if Path(p).exists()]
        images = [p for p in data.get("images", []) if Path(p).exists()]
        return {"voices": voices, "images": images}

    def is_ready(self) -> bool:
        media = self.load()
        return bool(media["voices"] or media["images"])

    # ------------------------------------------------------------------ #
    # Collection by forwarding media to the bot (private chat)
    # ------------------------------------------------------------------ #
    def begin_collection(self) -> None:
        """Start a fresh media set: wipe old files and reset buffers."""
        for path in MEDIA_DIR.glob("*"):
            try:
                path.unlink()
            except OSError:
                log.debug("Could not delete old media file {}", path)
        self._buf_voices = []
        self._buf_images = []

    async def add_from_message(self, message) -> str | None:
        """Download a forwarded voice/image from a message. Returns its kind."""
        doc = getattr(message, "document", None)
        mime = getattr(doc, "mime_type", "") or ""
        if message.voice or message.audio or mime.startswith("audio/"):
            path = MEDIA_DIR / f"voice_{len(self._buf_voices) + 1}.ogg"
            await message.download_media(file=str(path))
            self._buf_voices.append(str(path))
            return "voice"
        if message.photo or mime.startswith("image/"):
            path = MEDIA_DIR / f"img_{len(self._buf_images) + 1}.jpg"
            await message.download_media(file=str(path))
            self._buf_images.append(str(path))
            return "image"
        return None

    def commit_collection(self) -> dict:
        """Write the manifest from the collected buffers."""
        MANIFEST_PATH.write_text(
            json.dumps(
                {"voices": self._buf_voices, "images": self._buf_images},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        result = {"voices": len(self._buf_voices), "images": len(self._buf_images)}
        log.info("Media committed via forward: {}", result)
        return result

    @property
    def collected(self) -> dict:
        return {"voices": len(self._buf_voices), "images": len(self._buf_images)}

    # ------------------------------------------------------------------ #
    async def refresh(self, coordinator) -> dict:
        """Re-download voice + image media from the channel using one account.

        Returns a summary dict ``{"voices": int, "images": int, "error": str?}``.
        """
        if not coordinator.has_channel:
            return {"voices": 0, "images": 0, "error": "کانالی تنظیم نشده است."}

        account = await coordinator.first_active_account()
        if account is None:
            return {"voices": 0, "images": 0, "error": "اکانت فعالی موجود نیست."}

        voices: list[str] = []
        images: list[str] = []

        try:
            async with coordinator.account_client(account["session_name"]) as client:
                if not await client.is_user_authorized():
                    return {"voices": 0, "images": 0, "error": "اکانت غیرفعال است."}

                entity = await coordinator.get_channel_entity(client)
                async for message in client.iter_messages(entity, reverse=True):
                    if message.voice:
                        path = MEDIA_DIR / f"voice_{message.id}.ogg"
                        await client.download_media(message, file=str(path))
                        voices.append(str(path))
                    elif message.photo:
                        if len(images) >= MAX_IMAGES:
                            continue
                        path = MEDIA_DIR / f"img_{message.id}.jpg"
                        await client.download_media(message, file=str(path))
                        images.append(str(path))
        except Exception as exc:
            log.exception("Media refresh failed")
            return {"voices": len(voices), "images": len(images), "error": str(exc)}

        MANIFEST_PATH.write_text(
            json.dumps({"voices": voices, "images": images}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("Media refreshed: {} voice(s), {} image(s).", len(voices), len(images))
        return {"voices": len(voices), "images": len(images), "error": None}
