from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Mapping

from runtasks.telegram_errors import TelegramConfigurationError


_TOKEN_PATTERN = re.compile(r"[1-9][0-9]{5,}:[A-Za-z0-9_-]{20,}\Z")
_INTEGER_PATTERN = re.compile(r"-?[0-9]+\Z")


@dataclass(frozen=True)
class TelegramAuthorizationContext:
    user_id: int
    chat_id: int
    chat_type: str


@dataclass(frozen=True)
class TelegramAuthorizationCheck:
    user_allowed: bool
    chat_matches: bool

    @property
    def authorized(self) -> bool:
        return self.user_allowed and self.chat_matches

    def as_dict(self) -> dict[str, bool]:
        return {
            "authorized": self.authorized,
            "chat_matches": self.chat_matches,
            "user_allowed": self.user_allowed,
        }


@dataclass(frozen=True)
class TelegramDestination:
    chat_id: int = field(repr=False)
    thread_id: int | None = field(repr=False)

    @property
    def is_private(self) -> bool:
        return self.chat_id > 0

    @property
    def is_group(self) -> bool:
        return self.chat_id < 0


@dataclass(frozen=True)
class TelegramSettings:
    bot_token: str = field(repr=False)
    allowed_user_ids: tuple[int, ...] = field(repr=False)
    destination: TelegramDestination | None = field(repr=False)

    @property
    def redaction_values(self) -> tuple[str, ...]:
        values = [self.bot_token]
        values.extend(str(user_id) for user_id in self.allowed_user_ids)
        if self.destination is not None:
            values.append(str(self.destination.chat_id))
            if self.destination.thread_id is not None:
                values.append(str(self.destination.thread_id))
        return tuple(values)

    @property
    def has_authorization_configuration(self) -> bool:
        return bool(self.allowed_user_ids) and self.destination is not None

    def verify_authorization(
        self,
        context: TelegramAuthorizationContext,
    ) -> TelegramAuthorizationCheck:
        private_chat_matches_user = (
            context.chat_type == "private"
            and context.chat_id == context.user_id
            and self.destination is not None
            and context.chat_id == self.destination.chat_id
        )
        configured_group_matches = (
            context.chat_type in {"group", "supergroup"}
            and self.destination is not None
            and context.chat_id == self.destination.chat_id
        )
        return TelegramAuthorizationCheck(
            user_allowed=context.user_id in self.allowed_user_ids,
            chat_matches=private_chat_matches_user or configured_group_matches,
        )

    def authorizes(self, context: TelegramAuthorizationContext) -> bool:
        return self.verify_authorization(context).authorized


def load_telegram_settings(
    values: Mapping[str, str],
    *,
    require_destination: bool,
) -> TelegramSettings:
    token = values.get("RUNTASKS_TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise TelegramConfigurationError("Telegram bot token is missing")
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise TelegramConfigurationError("Telegram bot token is malformed")

    allowed_user_ids = _parse_allowed_user_ids(
        values.get("RUNTASKS_TELEGRAM_ALLOWED_USER_IDS")
    )
    notification_chat_id = _parse_optional_integer(
        values.get("RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID"),
        setting_name="Telegram notification chat ID",
        allow_negative=True,
    )
    thread_id = _parse_optional_integer(
        values.get("RUNTASKS_TELEGRAM_THREAD_ID"),
        setting_name="Telegram thread ID",
        allow_negative=False,
    )

    destination_input_present = any(
        value is not None and value.strip()
        for value in (
            values.get("RUNTASKS_TELEGRAM_ALLOWED_USER_IDS"),
            values.get("RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID"),
            values.get("RUNTASKS_TELEGRAM_THREAD_ID"),
        )
    )
    if require_destination or destination_input_present:
        if not allowed_user_ids:
            raise TelegramConfigurationError(
                "Telegram allowed user IDs are missing"
            )
        if notification_chat_id is None:
            raise TelegramConfigurationError(
                "Telegram notification chat ID is missing"
            )
        destination_is_private = notification_chat_id > 0
        if destination_is_private and notification_chat_id not in allowed_user_ids:
            raise TelegramConfigurationError(
                "private Telegram notification chat must belong to an allowed user"
            )
        if destination_is_private and thread_id is not None:
            raise TelegramConfigurationError(
                "Telegram thread ID cannot be used with a private chat"
            )

    destination = (
        None
        if notification_chat_id is None
        else TelegramDestination(
            chat_id=notification_chat_id,
            thread_id=thread_id,
        )
    )
    return TelegramSettings(
        bot_token=token,
        allowed_user_ids=allowed_user_ids,
        destination=destination,
    )


def _parse_allowed_user_ids(value: str | None) -> tuple[int, ...]:
    if value is None or not value.strip():
        return ()
    parsed: set[int] = set()
    for raw_id in value.split(","):
        item = raw_id.strip()
        if _INTEGER_PATTERN.fullmatch(item) is None:
            raise TelegramConfigurationError(
                "Telegram allowed user IDs must be comma-separated positive integers"
            )
        user_id = int(item)
        if user_id <= 0:
            raise TelegramConfigurationError(
                "Telegram allowed user IDs must be comma-separated positive integers"
            )
        parsed.add(user_id)
    return tuple(sorted(parsed))


def _parse_optional_integer(
    value: str | None,
    *,
    setting_name: str,
    allow_negative: bool,
) -> int | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    if _INTEGER_PATTERN.fullmatch(normalized) is None:
        raise TelegramConfigurationError(f"{setting_name} must be numeric")
    parsed = int(normalized)
    if parsed == 0 or (parsed < 0 and not allow_negative):
        qualifier = "a nonzero integer" if allow_negative else "a positive integer"
        raise TelegramConfigurationError(f"{setting_name} must be {qualifier}")
    return parsed
