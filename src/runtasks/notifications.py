from __future__ import annotations

from typing import Iterable, Protocol

from runtasks.redaction import redact_text


class NotificationError(RuntimeError):
    """Base class for safe notification-interface failures."""


class NotificationDeliveryError(NotificationError):
    """Raised when a transport cannot deliver a notification."""


class NotificationDestinationError(NotificationError, ValueError):
    """Raised when a configured notification destination is unusable."""


class NotificationClient(Protocol):
    """Application port for delivering a preconfigured outbound notification."""

    async def send(self, *, text: str) -> None: ...


class RedactingNotificationClient:
    """Redact outbound text and contain unsafe transport exceptions."""

    def __init__(
        self,
        client: NotificationClient,
        *,
        sensitive_values: Iterable[str] = (),
    ) -> None:
        self._client = client
        self._sensitive_values = tuple(sensitive_values)

    async def send(self, *, text: str) -> None:
        try:
            await self._client.send(
                text=redact_text(text, sensitive_values=self._sensitive_values)
            )
        except NotificationDestinationError:
            raise NotificationDestinationError(
                "notification destination is invalid"
            ) from None
        except Exception:
            raise NotificationDeliveryError("notification delivery failed") from None
