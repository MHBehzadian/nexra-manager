"""Runtime-tweakable campaign settings (persisted to data/campaign.json).

Currently holds the voice-delay range (gap between greeting and voice/images),
which the admin can change from the bot. Falls back to the defaults in
``content.py`` when the file is missing or invalid.
"""

from __future__ import annotations

import json
from pathlib import Path

from utils import get_logger

from . import content

log = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "data" / "campaign.json"


class CampaignConfig:
    """Small JSON-backed store for tweakable campaign parameters."""

    def __init__(self, path: Path | str = CONFIG_PATH) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.exception("Could not read campaign.json — using defaults")
            return {}

    def voice_delay(self) -> tuple[int, int]:
        """Return the (min, max) seconds gap between greeting and voice."""
        data = self._read()
        low = data.get("voice_delay_min")
        high = data.get("voice_delay_max")
        if isinstance(low, int) and isinstance(high, int) and 0 < low <= high:
            return (low, high)
        return content.GREETING_TO_VOICE

    def set_voice_delay(self, min_seconds: int, max_seconds: int) -> None:
        if min_seconds <= 0 or max_seconds < min_seconds:
            raise ValueError("Invalid voice-delay range")
        data = self._read()
        data["voice_delay_min"] = int(min_seconds)
        data["voice_delay_max"] = int(max_seconds)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("Voice delay set to {}–{}s", min_seconds, max_seconds)
