from __future__ import annotations

from runtasks.notifications import NotificationClient
from runtasks.telegram_config import (
    TelegramAuthorizationContext,
    TelegramDestination,
    TelegramSettings,
    load_telegram_settings,
)
from runtasks.telegram_errors import (
    PollerAlreadyRunningError,
    TelegramConfigurationError,
    TelegramDeliveryError,
    TelegramIntegrationError,
)
from runtasks.telegram_lock import PollerGuard
from runtasks.telegram_transport import build_telegram_notification_client
from runtasks.telegram_updates import (
    SetupCandidate,
    TelegramPoller,
    listen_for_authorization_checks,
)


TEST_NOTIFICATION_TEXT = (
    "RunTasks Telegram test succeeded. No credentials or task data were sent."
)


async def send_test_notification(client: NotificationClient) -> None:
    await client.send(text=TEST_NOTIFICATION_TEXT)


__all__ = [
    "PollerAlreadyRunningError",
    "PollerGuard",
    "SetupCandidate",
    "TelegramAuthorizationContext",
    "TelegramConfigurationError",
    "TelegramDeliveryError",
    "TelegramDestination",
    "TelegramIntegrationError",
    "TelegramPoller",
    "TelegramSettings",
    "build_telegram_notification_client",
    "listen_for_authorization_checks",
    "load_telegram_settings",
    "send_test_notification",
]
