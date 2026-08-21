from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from runtasks.paths import RuntimePaths


DEFAULT_TIMEZONE = "Asia/Singapore"
DEFAULT_DAILY_RUN_TIME = "09:00"


class ConfigurationError(ValueError):
    """Raised when non-secret application configuration is invalid."""


@dataclass(frozen=True)
class ConfigurationSource:
    path: Path | None = None

    @property
    def display_name(self) -> str:
        return "defaults" if self.path is None else str(self.path)


@dataclass(frozen=True)
class AppSettings:
    timezone: ZoneInfo
    daily_run_time: time
    source: ConfigurationSource

    @property
    def timezone_name(self) -> str:
        return str(self.timezone)

    @property
    def daily_run_time_text(self) -> str:
        return self.daily_run_time.strftime("%H:%M")


def load_app_settings(paths: RuntimePaths) -> AppSettings:
    if not paths.config_file.exists():
        return _settings_from_values(
            timezone_name=DEFAULT_TIMEZONE,
            daily_run_time_text=DEFAULT_DAILY_RUN_TIME,
            source=ConfigurationSource(),
        )

    try:
        with paths.config_file.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError("configuration file could not be read as TOML") from error

    timezone_name = raw.get("timezone", DEFAULT_TIMEZONE)
    daily_run_time_text = raw.get("daily_run_time", DEFAULT_DAILY_RUN_TIME)
    if not isinstance(timezone_name, str):
        raise ConfigurationError("timezone must be a string")
    if not isinstance(daily_run_time_text, str):
        raise ConfigurationError("daily_run_time must be a string")

    return _settings_from_values(
        timezone_name=timezone_name,
        daily_run_time_text=daily_run_time_text,
        source=ConfigurationSource(path=paths.config_file),
    )


def default_config_text() -> str:
    return (
        f'timezone = "{DEFAULT_TIMEZONE}"\n'
        f'daily_run_time = "{DEFAULT_DAILY_RUN_TIME}"\n'
    )


def _settings_from_values(
    *,
    timezone_name: str,
    daily_run_time_text: str,
    source: ConfigurationSource,
) -> AppSettings:
    try:
        timezone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ConfigurationError("timezone must name an installed IANA timezone") from error

    return AppSettings(
        timezone=timezone,
        daily_run_time=_parse_daily_run_time(daily_run_time_text),
        source=source,
    )


def _parse_daily_run_time(value: str) -> time:
    parts = value.split(":")
    if len(parts) != 2 or any(len(part) != 2 or not part.isdigit() for part in parts):
        raise ConfigurationError("daily_run_time must use 24-hour HH:MM format")
    hour, minute = (int(part) for part in parts)
    try:
        return time(hour=hour, minute=minute)
    except ValueError as error:
        raise ConfigurationError(
            "daily_run_time must use 24-hour HH:MM format"
        ) from error
