"""Conversation state management for multi-step bot flows.

Right now the only flow is *add account*, which walks through:

    NAME  ->  PHONE  ->  CODE  ->  (PASSWORD, only if 2FA)  ->  done

State is keyed by Telegram user id, so it naturally supports one active
conversation per user. The live Telethon client used during login is stored on
the conversation object because it must survive between messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any


class AddStep(Enum):
    NAME = auto()
    PHONE = auto()
    CODE = auto()
    PASSWORD = auto()


@dataclass
class AddAccountConversation:
    """Mutable state for an in-progress 'add account' flow."""

    step: AddStep = AddStep.NAME
    session_name: str | None = None
    phone: str | None = None
    phone_code_hash: str | None = None
    client: Any | None = None  # telethon.TelegramClient (kept connected mid-login)


@dataclass
class SetChannelConversation:
    """Single-step flow: waiting for the admin to send a channel identifier."""


@dataclass
class SetVoiceDelayConversation:
    """Single-step flow: waiting for the admin to send 'min max' minutes."""


@dataclass
class SetItemDelayConversation:
    """Single-step flow: waiting for 'min max' SECONDS between phase-2 items."""


@dataclass
class SetReportChannelConversation:
    """Single-step flow: waiting for the admin to send the report-channel id."""


@dataclass
class SetVoiceTextConversation:
    """Single-step flow: waiting for the admin to send the voice-accompanying text."""


@dataclass
class CollectMediaConversation:
    """The admin forwards voice/image messages to the bot; it saves them."""


# Any supported conversation type.
Conversation = (
    AddAccountConversation
    | SetChannelConversation
    | SetVoiceDelayConversation
    | SetItemDelayConversation
    | SetReportChannelConversation
    | SetVoiceTextConversation
    | CollectMediaConversation
)


class StateManager:
    """In-memory registry of active conversations, keyed by user id."""

    def __init__(self) -> None:
        self._states: dict[int, Conversation] = {}

    def get(self, user_id: int) -> Conversation | None:
        return self._states.get(user_id)

    def is_active(self, user_id: int) -> bool:
        return user_id in self._states

    def start_add(self, user_id: int) -> AddAccountConversation:
        conv = AddAccountConversation()
        self._states[user_id] = conv
        return conv

    def start_set_channel(self, user_id: int) -> SetChannelConversation:
        conv = SetChannelConversation()
        self._states[user_id] = conv
        return conv

    def start_set_voice_delay(self, user_id: int) -> SetVoiceDelayConversation:
        conv = SetVoiceDelayConversation()
        self._states[user_id] = conv
        return conv

    def start_set_item_delay(self, user_id: int) -> SetItemDelayConversation:
        conv = SetItemDelayConversation()
        self._states[user_id] = conv
        return conv

    def start_set_report_channel(self, user_id: int) -> SetReportChannelConversation:
        conv = SetReportChannelConversation()
        self._states[user_id] = conv
        return conv

    def start_set_voice_text(self, user_id: int) -> SetVoiceTextConversation:
        conv = SetVoiceTextConversation()
        self._states[user_id] = conv
        return conv

    def start_collect_media(self, user_id: int) -> CollectMediaConversation:
        conv = CollectMediaConversation()
        self._states[user_id] = conv
        return conv

    def clear(self, user_id: int) -> Conversation | None:
        return self._states.pop(user_id, None)
