from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, cast

import telegram

from tests.recorded_telegram import decode_recorded_update


_STATE_NAME = "TELEGRAM_FAKE_STATE"
_USER_HOME_NAME = "TELEGRAM_FAKE_USER_HOME"


@dataclass(frozen=True)
class FakeUser:
    id: int
    username: str | None = None


@dataclass(frozen=True)
class FakeChat:
    id: int
    type: str


@dataclass(frozen=True)
class FakeMessage:
    from_user: FakeUser | None
    chat: FakeChat
    text: str | None


@dataclass(frozen=True)
class FakeUpdate:
    update_id: int
    message: FakeMessage | None

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> FakeUpdate:
        recorded = decode_recorded_update(value)
        if (
            recorded.user_id is None
            or recorded.chat_id is None
            or recorded.chat_type is None
        ):
            return cls(update_id=recorded.update_id, message=None)
        return cls(
            update_id=recorded.update_id,
            message=FakeMessage(
                from_user=FakeUser(recorded.user_id),
                chat=FakeChat(recorded.chat_id, recorded.chat_type),
                text=recorded.text,
            ),
        )


@dataclass(frozen=True)
class FakeWebhookInfo:
    url: str


@dataclass(frozen=True)
class FakeFullChat:
    type: str
    is_forum: bool


@dataclass(frozen=True)
class FakeChatMember:
    status: str


class FakeBot:
    def __init__(self, token: str) -> None:
        self._token = token
        state_path = os.environ.get(_STATE_NAME)
        if state_path is None:
            raise RuntimeError("fake Telegram state is missing")
        self._state_path = Path(state_path)

    async def __aenter__(self) -> FakeBot:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exception_type, exception, traceback

    async def get_me(self) -> FakeUser:
        return FakeUser(id=123456789, username="runtasks_bot")

    async def get_webhook_info(self) -> FakeWebhookInfo:
        state = self._load_state()
        webhook_url = state.get("webhook_url", "")
        if not isinstance(webhook_url, str):
            raise RuntimeError("fixture webhook_url must be text")
        return FakeWebhookInfo(webhook_url)

    async def get_chat(self, chat_id: int) -> FakeFullChat:
        del chat_id
        state = self._load_state()
        chat_type = state.get("chat_type", "private")
        is_forum = state.get("is_forum", False)
        if not isinstance(chat_type, str) or not isinstance(is_forum, bool):
            raise RuntimeError("fixture chat metadata is malformed")
        return FakeFullChat(chat_type, is_forum)

    async def get_chat_member(
        self,
        chat_id: int,
        user_id: int,
    ) -> FakeChatMember:
        del chat_id
        state = self._load_state()
        raw_statuses = state.get("member_statuses", {})
        if not isinstance(raw_statuses, dict):
            raise RuntimeError("fixture member_statuses must be an object")
        statuses = cast(dict[str, object], raw_statuses)
        status = statuses.get(str(user_id), "left")
        if not isinstance(status, str):
            raise RuntimeError("fixture member status must be text")
        return FakeChatMember(status)

    async def get_updates(
        self,
        *,
        offset: int | None,
        timeout: int,
        read_timeout: int,
        allowed_updates: list[str],
    ) -> list[FakeUpdate]:
        del offset, timeout, read_timeout, allowed_updates
        state = self._load_state()
        raw_batches = state.get("update_batches", [])
        if not isinstance(raw_batches, list):
            raise RuntimeError("fixture update_batches must be a list")
        batches = cast(list[object], raw_batches)
        raw_updates = batches.pop(0) if batches else []
        state["update_batches"] = batches
        self._save_state(state)
        if not isinstance(raw_updates, list):
            raise RuntimeError("fixture update batch must be a list")
        return [
            FakeUpdate.from_dict(cast(dict[str, object], update))
            for update in raw_updates
            if isinstance(update, dict)
        ]

    async def send_chat_action(
        self,
        *,
        chat_id: int,
        action: str,
        message_thread_id: int | None,
    ) -> bool:
        del chat_id, action
        state = self._load_state()
        invalid_thread_ids = state.get("invalid_thread_ids", [])
        if not isinstance(invalid_thread_ids, list):
            raise RuntimeError("fixture invalid_thread_ids must be a list")
        if message_thread_id in invalid_thread_ids:
            raise telegram.error.BadRequest("message thread not found")
        return True

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        message_thread_id: int | None,
    ) -> None:
        state = self._load_state()
        if state.get("fail_send") is True:
            raise RuntimeError(
                "failed at "
                f"https://api.telegram.org/bot{self._token}/sendMessage"
            )
        raw_messages = state.get("sent_messages", [])
        if not isinstance(raw_messages, list):
            raise RuntimeError("fixture sent_messages must be a list")
        messages = cast(list[object], raw_messages)
        messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "thread_id": message_thread_id,
            }
        )
        state["sent_messages"] = messages
        self._save_state(state)

    def _load_state(self) -> dict[str, object]:
        value: object = json.loads(self._state_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("fake Telegram state must be an object")
        return cast(dict[str, object], value)

    def _save_state(self, state: dict[str, object]) -> None:
        self._state_path.write_text(json.dumps(state), encoding="utf-8")


if os.environ.get(_STATE_NAME) is not None:
    telegram.Bot = FakeBot  # type: ignore[misc,assignment]

fake_user_home = os.environ.get(_USER_HOME_NAME)
if fake_user_home is not None:
    import runtasks.paths as runtime_paths

    fake_user_home_path = Path(fake_user_home)
    runtime_paths._canonical_user_home = lambda: fake_user_home_path
