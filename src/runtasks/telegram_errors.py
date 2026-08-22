from __future__ import annotations


class TelegramIntegrationError(RuntimeError):
    """Raised when Telegram integration cannot complete an operation safely."""


class TelegramConfigurationError(TelegramIntegrationError, ValueError):
    """Raised when private Telegram configuration is missing or unsafe."""


class TelegramDeliveryError(TelegramIntegrationError):
    """Raised when Telegram cannot deliver or retrieve Bot API data."""


class PollerAlreadyRunningError(TelegramIntegrationError):
    """Raised when another process is polling updates for the same bot token."""
