from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import math
import re
import time
from pathlib import Path
from typing import AsyncIterator, Callable, Protocol, Sequence

from runtasks.telegram_config import (
    TelegramAuthorizationContext,
    TelegramSettings,
)
from runtasks.telegram_errors import TelegramConfigurationError
from runtasks.telegram_lock import PollerGuard


_START_COMMAND_PATTERN = re.compile(
    r"/start(?:@(?P<username>[A-Za-z0-9_]+))?\Z",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TelegramMessageRecord:
    user_id: int
    chat_id: int
    chat_type: str
    text: str | None


@dataclass(frozen=True)
class TelegramCallbackRecord:
    callback_id: str
    user_id: int
    chat_id: int | None
    chat_type: str | None
    message_id: int | None
    data: str | None

    @property
    def authorization_context(self) -> TelegramAuthorizationContext | None:
        if self.chat_id is None or self.chat_type is None:
            return None
        return TelegramAuthorizationContext(
            user_id=self.user_id,
            chat_id=self.chat_id,
            chat_type=self.chat_type,
        )


@dataclass(frozen=True)
class TelegramUpdateRecord:
    update_id: int
    message: TelegramMessageRecord | None
    callback: TelegramCallbackRecord | None = None


@dataclass(frozen=True)
class SetupCandidate:
    user_id: int
    chat_id: int
    chat_type: str

    @property
    def authorization_context(self) -> TelegramAuthorizationContext:
        return TelegramAuthorizationContext(
            user_id=self.user_id,
            chat_id=self.chat_id,
            chat_type=self.chat_type,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "user_id": self.user_id,
        }


@dataclass(frozen=True)
class SetupDiscovery:
    candidates: tuple[SetupCandidate, ...]
    authorization_mismatches: tuple[SetupCandidate, ...] = ()


class TelegramLongPollingClient(Protocol):
    async def get_webhook_url(self) -> str: ...


class TelegramUpdateClient(TelegramLongPollingClient, Protocol):
    async def get_bot_username(self) -> str: ...

    async def get_updates_transport(
        self,
        *,
        timeout_seconds: int,
        offset: int | None = None,
    ) -> Sequence[TelegramUpdateRecord]: ...


class _TelegramPollingSession:
    """Update source available only while its token-wide guard is held."""

    def __init__(self, client: TelegramUpdateClient) -> None:
        self._client = client

    async def get_bot_username(self) -> str:
        return await self._client.get_bot_username()

    async def get_webhook_url(self) -> str:
        return await self._client.get_webhook_url()

    async def get_updates(
        self,
        *,
        timeout_seconds: int,
        offset: int | None = None,
    ) -> Sequence[TelegramUpdateRecord]:
        return await self._client.get_updates_transport(
            timeout_seconds=timeout_seconds,
            offset=offset,
        )


class TelegramPoller:
    """Guard every application-level Bot API update-consumption session."""

    def __init__(self, lock_path: Path, client: TelegramUpdateClient) -> None:
        self._lock_path = lock_path
        self._client = client

    @asynccontextmanager
    async def session(self) -> AsyncIterator[_TelegramPollingSession]:
        with PollerGuard(self._lock_path):
            yield _TelegramPollingSession(self._client)

    async def discover_setup_candidates(
        self,
        *,
        timeout_seconds: int,
        on_ready: Callable[[], None] | None = None,
        candidate_filter: Callable[[SetupCandidate], bool] | None = None,
    ) -> SetupDiscovery:
        async with self.session() as session:
            return await _discover_setup_candidates(
                session,
                timeout_seconds=timeout_seconds,
                on_ready=on_ready,
                candidate_filter=candidate_filter,
            )


async def _discover_setup_candidates(
    client: _TelegramPollingSession,
    *,
    timeout_seconds: int,
    on_ready: Callable[[], None] | None = None,
    candidate_filter: Callable[[SetupCandidate], bool] | None = None,
) -> SetupDiscovery:
    if not 1 <= timeout_seconds <= 60:
        raise TelegramConfigurationError(
            "Telegram setup timeout must be between 1 and 60 seconds"
        )
    await verify_long_polling(client)
    bot_username = await client.get_bot_username()
    pending_updates = await client.get_updates(timeout_seconds=0, offset=-1)
    offset = _next_offset(pending_updates)
    if on_ready is not None:
        on_ready()

    deadline = time.monotonic() + timeout_seconds
    authorization_mismatches: list[SetupCandidate] = []
    while True:
        remaining = math.ceil(deadline - time.monotonic())
        if remaining <= 0:
            break
        updates = await client.get_updates(
            timeout_seconds=remaining,
            offset=offset,
        )
        candidates = setup_candidates_from_updates(
            updates,
            bot_username=bot_username,
        )
        if candidate_filter is not None:
            accepted: list[SetupCandidate] = []
            for candidate in candidates:
                if candidate_filter(candidate):
                    accepted.append(candidate)
                else:
                    authorization_mismatches.append(candidate)
            candidates = accepted
        if candidates:
            return SetupDiscovery(candidates=tuple(candidates))
        if not updates:
            break
        offset = _next_offset(updates)
        await asyncio.sleep(0)

    if authorization_mismatches:
        return SetupDiscovery(
            candidates=(),
            authorization_mismatches=tuple(
                dict.fromkeys(authorization_mismatches)
            ),
        )
    raise TelegramConfigurationError(
        "no /start update was found; send /start to the bot and try again"
    )


async def listen_for_authorization_checks(
    client: _TelegramPollingSession,
    settings: TelegramSettings,
    *,
    on_ready: Callable[[], None] | None = None,
    on_authorized: Callable[[], None] | None = None,
    max_batches: int | None = None,
) -> None:
    await verify_long_polling(client)
    bot_username = await client.get_bot_username()
    if on_ready is not None:
        on_ready()
    offset: int | None = None
    batches = 0
    while max_batches is None or batches < max_batches:
        updates = await client.get_updates(timeout_seconds=30, offset=offset)
        candidates = setup_candidates_from_updates(
            updates,
            bot_username=bot_username,
        )
        authorized = any(
            settings.authorizes(candidate.authorization_context)
            for candidate in candidates
        )
        if authorized and on_authorized is not None:
            on_authorized()
        next_offset = _next_offset(updates)
        if next_offset is not None:
            offset = next_offset
        batches += 1


async def verify_long_polling(client: TelegramLongPollingClient) -> None:
    webhook_url = await client.get_webhook_url()
    if webhook_url:
        raise TelegramConfigurationError(
            "Telegram webhook is configured; remove it before using long polling"
        )


def _next_offset(updates: Sequence[TelegramUpdateRecord]) -> int | None:
    if not updates:
        return None
    return max(update.update_id for update in updates) + 1


def setup_candidates_from_updates(
    updates: Sequence[TelegramUpdateRecord],
    *,
    bot_username: str,
) -> list[SetupCandidate]:
    candidates: list[SetupCandidate] = []
    seen: set[tuple[int, int]] = set()
    for update in updates:
        message = update.message
        if message is None or message.text is None:
            continue
        command = _START_COMMAND_PATTERN.fullmatch(message.text.strip())
        if command is None:
            continue
        addressed_username = command.group("username")
        if (
            addressed_username is not None
            and addressed_username.casefold() != bot_username.casefold()
        ):
            continue
        identity = (message.user_id, message.chat_id)
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append(
            SetupCandidate(
                user_id=message.user_id,
                chat_id=message.chat_id,
                chat_type=message.chat_type,
            )
        )
    return candidates
