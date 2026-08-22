from __future__ import annotations

from typing import Mapping, Sequence

from runtasks.telegram_updates import TelegramMessageRecord, TelegramUpdateRecord
from tests.recorded_telegram import decode_recorded_update


class FakeNotificationClient:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.messages: list[str] = []

    async def send(self, *, text: str) -> None:
        if self.failure is not None:
            raise self.failure
        self.messages.append(text)


class FakeTelegramClient:
    def __init__(
        self,
        *,
        updates: Sequence[dict[str, object]] = (),
        update_batches: Sequence[Sequence[dict[str, object]]] | None = None,
        webhook_url: str = "",
        bot_username: str = "runtasks_bot",
        failure: Exception | None = None,
    ) -> None:
        self.updates = list(updates)
        self.update_batches = (
            None
            if update_batches is None
            else [list(batch) for batch in update_batches]
        )
        self.webhook_url = webhook_url
        self.bot_username = bot_username
        self.failure = failure
        self.sent: list[tuple[int, str, int | None]] = []
        self.poll_requests: list[tuple[int, int | None]] = []

    async def get_bot_username(self) -> str:
        return self.bot_username

    async def get_webhook_url(self) -> str:
        return self.webhook_url

    async def get_updates_transport(
        self,
        *,
        timeout_seconds: int,
        offset: int | None = None,
    ) -> list[TelegramUpdateRecord]:
        self.poll_requests.append((timeout_seconds, offset))
        if self.failure is not None:
            raise self.failure
        if self.update_batches is not None:
            updates = self.update_batches.pop(0) if self.update_batches else []
        else:
            updates = self.updates
        return [_recorded_update(update) for update in updates]

    async def send_message(
        self,
        *,
        destination: int,
        text: str,
        thread_id: int | None = None,
    ) -> None:
        if self.failure is not None:
            raise self.failure
        self.sent.append((destination, text, thread_id))


def _recorded_update(update: Mapping[str, object]) -> TelegramUpdateRecord:
    recorded = decode_recorded_update(update)
    if (
        recorded.user_id is None
        or recorded.chat_id is None
        or recorded.chat_type is None
    ):
        return TelegramUpdateRecord(recorded.update_id, None)
    return TelegramUpdateRecord(
        recorded.update_id,
        TelegramMessageRecord(
            user_id=recorded.user_id,
            chat_id=recorded.chat_id,
            chat_type=recorded.chat_type,
            text=recorded.text,
        ),
    )
