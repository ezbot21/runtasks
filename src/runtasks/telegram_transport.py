from __future__ import annotations

from typing import Iterable, Protocol, Sequence

from telegram import Bot, Update
from telegram.constants import ChatAction, ChatMemberStatus, ChatType
from telegram.error import BadRequest, Forbidden

from runtasks.notifications import (
    NotificationClient,
    NotificationDestinationError,
    RedactingNotificationClient,
)
from runtasks.telegram_config import TelegramDestination, TelegramSettings
from runtasks.telegram_errors import (
    TelegramConfigurationError,
    TelegramDeliveryError,
)
from runtasks.telegram_updates import (
    TelegramMessageRecord,
    TelegramUpdateClient,
    TelegramUpdateRecord,
)


class TelegramMessageClient(Protocol):
    async def send_message(
        self,
        *,
        destination: int,
        text: str,
        thread_id: int | None = None,
    ) -> None: ...


class _TelegramNotificationClient(NotificationClient):
    """Bind Telegram-specific addressing behind the notification port."""

    def __init__(
        self,
        client: TelegramMessageClient,
        destination: TelegramDestination,
    ) -> None:
        self._client = client
        self._destination = destination

    async def send(self, *, text: str) -> None:
        await self._client.send_message(
            destination=self._destination.chat_id,
            text=text,
            thread_id=self._destination.thread_id,
        )


def build_telegram_notification_client(
    client: TelegramMessageClient,
    destination: TelegramDestination,
    *,
    sensitive_values: Iterable[str],
) -> NotificationClient:
    return RedactingNotificationClient(
        _TelegramNotificationClient(client, destination),
        sensitive_values=sensitive_values,
    )


class PythonTelegramBotClient(TelegramMessageClient, TelegramUpdateClient):
    """Adapter for the exactly pinned python-telegram-bot Bot API client."""

    def __init__(self, token: str) -> None:
        self._bot = Bot(token=token)

    async def get_bot_username(self) -> str:
        try:
            async with self._bot:
                bot = await self._bot.get_me()
        except Exception:
            raise TelegramDeliveryError(
                "Telegram bot identity could not be verified"
            ) from None
        if bot.username is None:
            raise TelegramDeliveryError(
                "Telegram bot identity is incomplete"
            )
        return bot.username

    async def get_webhook_url(self) -> str:
        try:
            async with self._bot:
                webhook = await self._bot.get_webhook_info()
        except Exception:
            raise TelegramDeliveryError(
                "Telegram configuration could not be verified"
            ) from None
        return webhook.url

    async def get_updates_transport(
        self,
        *,
        timeout_seconds: int,
        offset: int | None = None,
    ) -> Sequence[TelegramUpdateRecord]:
        try:
            async with self._bot:
                updates = await self._bot.get_updates(
                    offset=offset,
                    timeout=timeout_seconds,
                    read_timeout=timeout_seconds + 5,
                    allowed_updates=["message"],
                )
        except Exception:
            raise TelegramDeliveryError(
                "Telegram updates could not be read"
            ) from None
        return [_normalize_update(update) for update in updates]

    async def verify_destination(self, settings: TelegramSettings) -> None:
        destination = settings.destination
        if destination is None:
            raise TelegramConfigurationError(
                "Telegram notification chat ID is missing"
            )
        try:
            async with self._bot:
                chat = await self._bot.get_chat(destination.chat_id)
                administrator_found = False
                if chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
                    for user_id in settings.allowed_user_ids:
                        member = await self._bot.get_chat_member(
                            destination.chat_id,
                            user_id,
                        )
                        if member.status in {
                            ChatMemberStatus.ADMINISTRATOR,
                            ChatMemberStatus.OWNER,
                        }:
                            administrator_found = True
                            break
        except (BadRequest, Forbidden):
            raise NotificationDestinationError(
                "Telegram notification destination is invalid or inaccessible"
            ) from None
        except Exception:
            raise TelegramDeliveryError(
                "Telegram notification destination could not be verified"
            ) from None

        if destination.is_private and chat.type != ChatType.PRIVATE:
            raise NotificationDestinationError(
                "positive Telegram notification destination must be a private chat"
            )
        if destination.is_group and chat.type not in {
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        }:
            raise NotificationDestinationError(
                "negative Telegram notification destination must be a group"
            )
        if destination.is_group and not administrator_found:
            raise NotificationDestinationError(
                "Telegram group must have an allowed user as administrator"
            )
        if destination.thread_id is not None and (
            chat.type != ChatType.SUPERGROUP or not chat.is_forum
        ):
            raise NotificationDestinationError(
                "Telegram thread destination must be a forum supergroup"
            )
        if destination.thread_id is not None:
            try:
                async with self._bot:
                    await self._bot.send_chat_action(
                        chat_id=destination.chat_id,
                        action=ChatAction.TYPING,
                        message_thread_id=destination.thread_id,
                    )
            except (BadRequest, Forbidden):
                raise NotificationDestinationError(
                    "Telegram notification thread is invalid or inaccessible"
                ) from None
            except Exception:
                raise TelegramDeliveryError(
                    "Telegram notification thread could not be verified"
                ) from None

    async def send_message(
        self,
        *,
        destination: int,
        text: str,
        thread_id: int | None = None,
    ) -> None:
        try:
            async with self._bot:
                await self._bot.send_message(
                    chat_id=destination,
                    text=text,
                    message_thread_id=thread_id,
                )
        except BadRequest:
            raise NotificationDestinationError(
                "Telegram notification destination or thread is invalid"
            ) from None
        except Exception:
            raise TelegramDeliveryError(
                "Telegram notification delivery failed"
            ) from None


def _normalize_update(update: Update) -> TelegramUpdateRecord:
    message = update.message
    if message is None or message.from_user is None:
        return TelegramUpdateRecord(update_id=update.update_id, message=None)
    return TelegramUpdateRecord(
        update_id=update.update_id,
        message=TelegramMessageRecord(
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            chat_type=message.chat.type,
            text=message.text,
        ),
    )
