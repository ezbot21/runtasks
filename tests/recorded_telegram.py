from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping, cast


TELEGRAM_UPDATES_FIXTURE = Path(__file__).parent / "fixtures" / "telegram_updates.json"


@dataclass(frozen=True)
class RecordedTelegramUpdate:
    update_id: int
    user_id: int | None
    chat_id: int | None
    chat_type: str | None
    text: str | None


def load_recorded_updates() -> list[dict[str, object]]:
    value: object = json.loads(
        TELEGRAM_UPDATES_FIXTURE.read_text(encoding="utf-8")
    )
    if not isinstance(value, list) or not all(
        isinstance(update, dict) for update in value
    ):
        raise ValueError("recorded Telegram updates must be a list of objects")
    return [cast(dict[str, object], update) for update in value]


def decode_recorded_update(
    value: Mapping[str, object],
) -> RecordedTelegramUpdate:
    update_id = value.get("update_id")
    if not isinstance(update_id, int):
        raise ValueError("fixture update_id must be numeric")
    raw_message = value.get("message")
    if not isinstance(raw_message, Mapping):
        return RecordedTelegramUpdate(update_id, None, None, None, None)
    message = cast(Mapping[str, object], raw_message)
    raw_sender = message.get("from")
    raw_chat = message.get("chat")
    if not isinstance(raw_sender, Mapping) or not isinstance(raw_chat, Mapping):
        return RecordedTelegramUpdate(update_id, None, None, None, None)
    sender = cast(Mapping[str, object], raw_sender)
    chat = cast(Mapping[str, object], raw_chat)
    user_id = sender.get("id")
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    text = message.get("text")
    return RecordedTelegramUpdate(
        update_id=update_id,
        user_id=user_id if isinstance(user_id, int) else None,
        chat_id=chat_id if isinstance(chat_id, int) else None,
        chat_type=chat_type if isinstance(chat_type, str) else None,
        text=text if isinstance(text, str) else None,
    )
